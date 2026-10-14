"""Run meteo questions through ap-explanation itself, unchanged, on a ProvSQL database, and
record for each one what the demo would get: whether the service answered, whether its
answer is the plain SQL one, the peak memory of the database backends and of the database's
container, and the server processes the kernel killed.

Unlike scripts/provsql_compare.py, nothing is rewritten here: the question's SQL goes in an
AP to the service, which annotates the tables and asks for every semiring, as the demo does.
Every question runs in a database of its own, started from a snapshot the service annotated,
and capped at 3 GB.

Setup, one snapshot per ProvSQL version (images as in scripts/provsql_compare.py):
    python3 scripts/apx_service_check.py prepare provsql:master-fix apx-meteo:1.13
    python3 scripts/apx_service_check.py prepare ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0 apx-meteo:1.12
    # The demo's data holds the City of Zurich cell only: with --regions, its readings and
    # elevation points are copied to every cell of Attica, Canton of Bern and Peloponnese,
    # which is what the memory estimates of the Summary tab assume.
    python3 scripts/apx_service_check.py prepare provsql:master-fix apx-regions:1.13 --regions
    # the readings only (the elevation points stay those of the City of Zurich cell), 6 times smaller
    python3 scripts/apx_service_check.py prepare provsql:master-fix apx-regions-readings:1.13 --regions=readings

Usage:
    python3 scripts/apx_service_check.py run apx-meteo:1.13 1.13 zurich > assets/apx_service_113.jsonl
    python3 scripts/apx_service_check.py run apx-regions:1.13 1.13 <ids> --max-memory=1GB --set=temp_file_limit=3GB --reuse
    python3 scripts/apx_service_check.py run apx-meteo:1.13 1.13 532 536

``zurich`` runs the City of Zurich question of every template. ``--max-memory`` sets
``provsql.max_memory`` on the database (ProvSQL 1.13; the service does not set it, and it is
unlimited by default), ``--set=name=value`` any other setting (the --regions joins spill tens of
GB to disk: ``--set=temp_file_limit=3GB`` kept them within the test machine's disk). ``--shard=i/n`` runs every n-th question from the i-th: set WORKER to
run several shards at once, each with its own database, Redis and service. ``--reuse`` keeps
one database for every question (see :data:`_RESTORE_STORE`), as the --regions snapshot needs.
"""
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from memtest_questions import ap_for  # noqa: E402

ROOT = Path(__file__).parent.parent
QUESTIONS = ROOT / "assets" / "meteo_queries.csv"
APX_IMAGE = os.environ.get("APX_IMAGE", "ghcr.io/datagems-eosc/datagems-eosc/ap-explanation:v1.2.0")
WORKER = os.environ.get("WORKER", "0")
NETWORK, DB, REDIS, APX = (f"apxcheck{WORKER}", f"apxcheck-db{WORKER}", f"apxcheck-redis{WORKER}",
                           f"apxcheck-apx{WORKER}")
DEADLINE_SECONDS = 180
PLAIN_TIMEOUT_SECONDS = 300
ZURICH = (47.4, 8.5)
ALL_TABLES = ("meteo_elevation", "meteo_tmin", "meteo_tmax", "meteo_tp", "meteo_windspeedmax", "meteo_tmean",
              "meteo_sdmax", "meteo_ssrd", "landcover_upscaled", "landcover_type")
_CELL_RE = re.compile(r"\((\d+\.\d), (\d+\.\d)\)")
_KILLED_RE = re.compile(r'(server process|background worker "[^"]+") \(PID \d+\) was terminated by signal (\d+)')


def docker(*args: str, check: bool = False, **kw) -> subprocess.CompletedProcess:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=False, **kw)
    if check and done.returncode:
        raise SystemExit(f"docker {' '.join(args)[:120]} failed: {done.stderr[-1000:]}")
    return done


def address(container: str) -> str:
    return docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container).stdout.strip()


def connect(host: str, **kw) -> psycopg.Connection:
    return psycopg.connect(host=host, user="provdemo", password="provdemo", dbname="meteo",
                           autocommit=True, connect_timeout=5, **kw)


def wait_db(host_of: callable) -> str:
    for _ in range(1200):
        host = host_of()
        try:
            conn = connect(host)
            # The image's entrypoint upgrades the extension once the server accepts connections.
            upgrading = conn.execute("SELECT installed_version <> default_version FROM pg_available_extensions "
                                     "WHERE name = 'provsql'").fetchone()[0]
            conn.close()
            if not upgrading:
                return host
        except psycopg.Error:
            pass
        time.sleep(0.5)
    raise SystemExit("the database did not start")


def call(url: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"}, method="POST" if body else "GET")
    return json.load(urllib.request.urlopen(request, timeout=30))


def service() -> str:
    """The service's base URL, starting it (and its Redis) if needed."""
    docker("network", "create", NETWORK)
    if docker("inspect", "-f", "{{.State.Running}}", REDIS).stdout.strip() != "true":
        docker("rm", "-f", "-v", REDIS)
        docker("run", "-d", "--name", REDIS, "--network", NETWORK, "redis:7-alpine", check=True)
    if docker("inspect", "-f", "{{.State.Running}}", APX).stdout.strip() != "true":
        docker("rm", "-f", "-v", APX)
        redis = f"redis://{REDIS}:6379/0"
        docker("run", "-d", "--name", APX, "--network", NETWORK, "-e", f"POSTGRES_HOST={DB}",
               "-e", "POSTGRES_USER=provdemo", "-e", "POSTGRES_PASSWORD=provdemo",
               "-e", f"CELERY_BROKER_URL={redis}", "-e", f"CELERY_RESULT_BACKEND={redis}",
               "-e", f"REDIS_BROKER_URI={redis}", "-e", "OIDC_ISSUER=", APX_IMAGE, check=True)
    base = f"http://{address(APX)}:5000/api/v1"
    for _ in range(240):
        try:
            call(f"{base}/health")
            return base
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"ap-explanation did not start: {docker('logs', '--tail', '20', APX).stderr}")


def explain(base: str, row: dict, deadline: int, on_poll: callable = lambda: None) -> dict:
    """Send the question's AP, as the demo does (every semiring, no probability), and wait."""
    docker("exec", REDIS, "redis-cli", "FLUSHALL")  # no cached result of an earlier run
    t0 = time.monotonic()
    task_id = call(f"{base}/aps/explanation?probability=false", ap_for(row))["task_id"]
    while time.monotonic() - t0 < deadline:
        time.sleep(0.3)
        on_poll()
        polled = call(f"{base}/aps/explanation/{task_id}")
        if polled["status"] not in ("pending", "started", "retry"):
            return polled | {"seconds": round(time.monotonic() - t0, 1)}
    return {"status": "timeout", "error": f"no result after {deadline} s", "seconds": round(time.monotonic() - t0, 1)}


def region_cells() -> list[tuple[str, str]]:
    """The cells of Attica, Canton of Bern and Peloponnese, as listed by the questions of template A1."""
    with QUESTIONS.open(newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r["category"], r["template_index"]) == ("A", "1")]
    return sorted({c for r in rows for c in _CELL_RE.findall(r["sql"])} - {tuple(map(str, ZURICH))})


def regions_sql(elevation: bool = True) -> str:
    """Copies the City of Zurich cell's readings, and elevation points, to every region cell."""
    values = ", ".join(f"({lat}, {lon})" for lat, lon in region_cells())
    lat0, lon0 = ZURICH
    sql = [f"CREATE TEMP TABLE cells (lat numeric, lon numeric); INSERT INTO cells VALUES {values};"]
    for table, column in (("meteo_tmin", "tmin"), ("meteo_tmax", "tmax"), ("meteo_tp", "tp"),
                          ("meteo_windspeedmax", "windspeedmax")):
        sql.append(f"INSERT INTO meteo.{table} SELECT c.lat::real, c.lon::real, z.time, z.{column} "
                   f"FROM meteo.{table} z, cells c;")
    if elevation:
        sql.append("INSERT INTO meteo.meteo_elevation SELECT (z.latitude::numeric + c.lat - {0})::real, "
                   "(z.longitude::numeric + c.lon - {1})::real, z.elevation FROM meteo.meteo_elevation z, cells c;"
                   .format(lat0, lon0))
    return "\n".join(sql) + "\n"


def prepare(base_image: str, tag: str, regions: str | None) -> None:
    docker("rm", "-f", "-v", DB)
    docker("network", "create", NETWORK)
    docker("run", "-d", "--name", DB, "--network", NETWORK, "-e", "PGDATA=/pgdata", "-e", "POSTGRES_USER=provdemo",
           "-e", "POSTGRES_PASSWORD=provdemo", "-e", "POSTGRES_DB=meteo", base_image, check=True)
    while "PostgreSQL init process complete" not in (lambda p: p.stdout + p.stderr)(docker("logs", DB)):
        time.sleep(1)
    host = wait_db(lambda: address(DB))
    docker("cp", f"{ROOT}/dependencies/postgres-seed/.",f"{DB}:/docker-entrypoint-initdb.d/", check=True)
    psql = ["exec", "-i", DB, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "provdemo", "-d", "meteo"]
    docker(*psql, "-f", "/docker-entrypoint-initdb.d/01_meteo.sql", check=True)
    if regions:
        docker(*psql, input=regions_sql(elevation=regions != "readings"), check=True)
    docker(*psql, input=(ROOT / "scripts/meteo_standins.sql").read_text(), check=True)
    print("seeded", file=sys.stderr)
    # The service annotates the tables of the AP before it runs the query: one AP over all
    # of them annotates the snapshot as it would.
    row = {"question_id": "annotate", "sql": "SELECT tmin FROM meteo_tmin LIMIT 1", "tables": ";".join(ALL_TABLES)}
    result = explain(service(), row, 4 * 3600)
    print("annotated:", result["status"], result["seconds"], "s", str(result.get("error") or "")[:200], file=sys.stderr)
    conn = connect(host)
    conn.execute("VACUUM (FREEZE, ANALYZE)")  # no hint bit left to write: runs do not copy the tables up
    conn.execute("CHECKPOINT")
    conn.close()
    docker("stop", DB, check=True)
    docker("commit", DB, tag, check=True)
    docker("rm", "-v", DB)


# With --reuse, one database serves every question, restarted between two of them: a
# container started from a large snapshot copies every table file it writes to (hint bits,
# autovacuum), 44 GB for the --regions one, too much to do once per question. ProvSQL's
# circuit store, the only state a query leaves, is put back as the snapshot had it on each
# start, so that every question still starts from a fresh store.
_RESTORE_STORE = (
    "set -e; s=/pgdata/store0; if [ ! -d $s ]; then mkdir $s; f=$(ls /pgdata/base/*/provsql_gates.mmap); "
    "dirname $f > $s.dir; cp -p $(dirname $f)/provsql_*.mmap $s/; fi; cp -p $s/* $(cat $s.dir)/; "
    "exec /usr/local/bin/provsql-entrypoint.sh postgres -c config_file=/etc/postgresql/postgresql.conf")
REUSE = False
_started = [""]  # when the database last started: its log since then is this question's


def fresh(snapshot: str, settings: list[str]) -> str:
    running = docker("inspect", "-f", "{{.State.Running}}", DB).stdout.strip() == "true"
    _started[0] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if REUSE and running:
        docker("restart", "-t", "30", DB, check=True)
    else:
        docker("rm", "-f", "-v", DB)
        docker("network", "create", NETWORK)
        entrypoint = ["--entrypoint", "sh", snapshot, "-c", _RESTORE_STORE] if REUSE else [snapshot]
        docker("run", "-d", "--name", DB, "--network", NETWORK, "--memory=3g", "--memory-swap=3g", *entrypoint,
               check=True)
    host = wait_db(lambda: address(DB))
    conn = connect(host)
    for setting in settings:
        conn.execute(f"ALTER DATABASE meteo SET {setting}")
    conn.close()
    return host


def processes_lost() -> list[str]:
    logs = docker("logs", "--since", _started[0], DB)
    return [f"{who}, signal {sig}" for who, sig in _KILLED_RE.findall(logs.stdout + logs.stderr)]


def normalised(rows: list) -> list:
    def cell(value: object) -> str:
        text = re.sub(r" \(\*\)$", "", str(value))  # an aggregate prints as '13 (*)'
        try:
            return f"{float(text):.9g}"
        except ValueError:
            return text
    return sorted(tuple(cell(v) for v in row) for row in rows)


def plain_answer(host: str, sql: str, record: dict) -> list | None:
    """The question's plain SQL answer, ProvSQL off, or None (recorded in *record*)."""
    plain = connect(host)
    plain.execute("SET search_path TO meteo, public, provsql")  # as the service does
    plain.execute("SET provsql.active = 0")
    plain.execute(f"SET statement_timeout = '{PLAIN_TIMEOUT_SECONDS}s'")
    # The joins of the --regions data spill to disk: tens of GB in 5 minutes, without a cap
    # (per process, hence no parallel workers).
    plain.execute("SET temp_file_limit = '4GB'")
    plain.execute("SET max_parallel_workers_per_gather = 0")
    try:
        rows = plain.execute(sql).fetchall()
        record["plain_rows"] = len(rows)
        return rows
    except psycopg.Error as e:
        record["plain_error"] = " ".join(str(e).split())[:200]
        return None
    finally:
        plain.close()


def measure(snapshot: str, label: str, row: dict, settings: list[str]) -> dict:
    record = {"run": label, "question_id": row["question_id"], "settings": settings}
    host = fresh(snapshot, settings)
    # With --reuse (the --regions data), the plain answer, minutes long on its joins, is only
    # computed to check an answer of the service.
    plain_rows = None if REUSE else plain_answer(host, row["sql"], record)

    monitor, peak = connect(host), [0]

    def sample() -> None:
        try:
            kb = monitor.execute(
                r"SELECT coalesce(max(substring(pg_read_file('/proc/' || pid || '/status', true) "
                r"FROM 'VmHWM:\s+(\d+) kB')::bigint), 0) FROM pg_stat_activity "
                r"WHERE datname = 'meteo' AND backend_type = 'client backend' AND pid <> pg_backend_pid()").fetchone()[0]
            peak[0] = max(peak[0], kb)
        except psycopg.Error:
            pass

    result = explain(service(), row, DEADLINE_SECONDS, sample)
    if result["status"] == "timeout":  # the query goes on in the database: only a restart stops it
        docker("restart", "-t", "2", DB)
    else:
        time.sleep(1)  # leave the server the time to log what it lost
    container_peak = docker("exec", DB, "cat", "/sys/fs/cgroup/memory.peak").stdout.strip()
    derivations = (result.get("result") or {}).get("derivations") or []
    record |= {"status": result["status"], "seconds": result["seconds"],
               "error": " ".join(str(result.get("error") or "").split())[:300],
               "peak_backend_mb": round(peak[0] / 1024),
               "container_peak_mb": round(int(container_peak) / 2**20) if container_peak.isdigit() else None,
               "lost": processes_lost(), "rows": len(derivations),
               "semirings": sorted({s for d in derivations for s in (d.get("provenance") or {})}),
               "with_formula": sum(1 for d in derivations
                                   if ((d.get("provenance") or {}).get("formula") or {}).get("expression"))}
    if result["status"] == "success" and REUSE:
        plain_rows = plain_answer(wait_db(lambda: address(DB)), row["sql"], record)
    if result["status"] == "success" and plain_rows is not None:
        record["matches_plain"] = normalised([list(d["answer"].values()) for d in derivations]) == normalised(plain_rows)
    monitor.close()
    return record


def run(snapshot: str, label: str, args: list[str]) -> None:
    settings = [f"provsql.max_memory = '{a.split('=', 1)[1]}'" for a in args if a.startswith("--max-memory=")]
    settings += ["{} = '{}'".format(*a.split("=", 1)[1].split("=", 1)) for a in args if a.startswith("--set=")]
    shard = next((tuple(map(int, a.split("=", 1)[1].split("/"))) for a in args if a.startswith("--shard=")), (0, 1))
    global REUSE
    REUSE = "--reuse" in args
    targets = [a for a in args if not a.startswith("--")]
    with QUESTIONS.open(newline="") as f:
        rows = {r["question_id"]: r for r in csv.DictReader(f)}
    ids = [q for q, r in rows.items() if "City of Zurich" in r["question"]] if targets == ["zurich"] else targets
    for question_id in ids[shard[0]::shard[1]]:
        record = measure(snapshot, label, rows[question_id], settings)
        print(json.dumps(record), flush=True)
        print(question_id, record["status"], record.get("matches_plain"), record["peak_backend_mb"],
              record["lost"], record["error"][:100], file=sys.stderr, flush=True)


if __name__ == "__main__":
    command, *rest = sys.argv[1:]
    try:
        if command == "prepare":
            prepare(rest[0], rest[1], next((a.partition("=")[2] or "all" for a in rest if a.startswith("--regions")), None))
        else:
            run(rest[0], rest[1], rest[2:])
    finally:
        for container in (DB, APX, REDIS):
            docker("rm", "-f", "-v", container)
