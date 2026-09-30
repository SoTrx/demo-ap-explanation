#!/bin/bash
# Measure how ProvSQL's formula evaluation of a comparison on an aggregate grows with the
# number of readings in the compared group: for each n, a table of n readings, then
#   SELECT sr_formula(provenance(), map) FROM (SELECT g, SUM(v) s FROM t GROUP BY g) x WHERE s > 0.001
# the shape of Q295, with the peak memory of the backend (VmHWM) sampled every 0.2 s.
#
# Runs in a throwaway, memory-capped container, so an out-of-memory kill stays there:
#   docker run -d --name provsql-memtest --memory=3g --memory-swap=3g \
#     -e POSTGRES_USER=provdemo -e POSTGRES_PASSWORD=provdemo -e POSTGRES_DB=memtest \
#     ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0
#   scripts/memtest_group_size.sh > assets/memory_group_size.csv
#   docker rm -f provsql-memtest
# ProvSQL ignores cancellation while evaluating, so a run over TIMEOUT restarts the container.
C=${CONTAINER:-provsql-memtest}
TIMEOUT=${TIMEOUT:-90}
export LC_NUMERIC=C

q() { docker exec "$C" env PGOPTIONS=-csearch_path=public,provsql psql -U provdemo -d memtest -Atq -c "$1" 2>&1; }

wait_ready() {
  for _ in $(seq 60); do q "SELECT 1" | grep -qx 1 && return; sleep 1; done
}

run() {  # run <n> <wet|dry>: wet readings are all non-zero, dry ones 4 in 5 at zero
  local n=$1 kind=$2 t="t_${2}_$1" vals
  if [ "$kind" = wet ]; then vals="1 + (i % 9)"; else vals="CASE WHEN i % 5 = 0 THEN 1 + (i % 9) ELSE 0 END"; fi
  q "SET provsql.active = 1; DROP TABLE IF EXISTS $t CASCADE;
     CREATE TABLE $t AS SELECT 1 AS g, i AS id, ($vals)::float8 / 1000 AS v FROM generate_series(1, $n) i;
     SELECT add_provenance('$t'); SELECT create_provenance_mapping('${t}_map', '$t', 'id');" >/dev/null
  docker exec -d "$C" sh -c "PGOPTIONS=-csearch_path=public,provsql PGAPPNAME=memtest_$t psql -U provdemo -d memtest -Atq \
    -c \"SET provsql.active = 1; SELECT length(sr_formula(provenance(), '${t}_map')) FROM (SELECT g, SUM(v) AS s FROM $t GROUP BY g) x WHERE s > 0.001\" \
    > /tmp/out_$t 2>&1; echo done >> /tmp/out_$t"
  local t0 peak=0 status=timeout out hwm
  t0=$(date +%s.%N)
  for _ in $(seq $((TIMEOUT * 5))); do
    sleep 0.2
    hwm=$(q "SELECT substring(pg_read_file('/proc/' || pid || '/status', true) FROM 'VmHWM:\s+(\d+) kB') FROM pg_stat_activity WHERE application_name = 'memtest_$t'")
    [[ "$hwm" =~ ^[0-9]+$ ]] && (( hwm > peak )) && peak=$hwm
    out=$(docker exec "$C" cat "/tmp/out_$t" 2>/dev/null)
    if [[ "$out" == *done* ]]; then
      if [[ "$out" == *"server closed"* || "$out" == *terminated* ]]; then status=killed; else status=ok; fi
      break
    fi
  done
  local chars=""
  [ "$status" = ok ] && chars=$(echo "$out" | head -1 | cut -d'|' -f1)
  printf '%s,%s,%s,%s,%.1f,%s\n' "$n" "$kind" "$status" "$chars" "$(echo "$(date +%s.%N) - $t0" | bc)" "$((peak / 1024))"
  [ "$status" = timeout ] && docker restart "$C" >/dev/null
  [ "$status" != ok ] && sleep 5 && wait_ready
}

wait_ready
echo "readings,values,status,formula_chars,seconds,peak_backend_mb"
for n in ${SIZES:-4 8 12 16 18 20 22 24}; do run "$n" wet; done
for n in ${DRY_SIZES:-16 18 20 31}; do run "$n" dry; done
