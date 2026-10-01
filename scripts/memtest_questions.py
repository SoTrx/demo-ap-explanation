"""Run meteo questions through ap-explanation on a memory-capped database, and record for
each one its outcome, time and the peak memory of the database backends serving it.

Needs a throwaway stack, so that an out-of-memory kill stays there:
    docker network create memtest-net
    docker run -d --name provsql-memtest --network memtest-net --memory=3g --memory-swap=3g \\
      -e POSTGRES_USER=provdemo -e POSTGRES_PASSWORD=provdemo -e POSTGRES_DB=memtest \\
      ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0
    docker cp dependencies/postgres-seed/. provsql-memtest:/docker-entrypoint-initdb.d/
    docker exec provsql-memtest psql -U provdemo -d memtest -c "CREATE DATABASE meteo"
    docker exec provsql-memtest psql -U provdemo -d meteo -f /docker-entrypoint-initdb.d/01_meteo.sql
    docker run -d --name memtest-redis --network memtest-net redis:7-alpine
    docker run -d --name memtest-apx --network memtest-net -p 5011:5000 \\
      -e POSTGRES_HOST=provsql-memtest -e POSTGRES_USER=provdemo -e POSTGRES_PASSWORD=provdemo \\
      -e CELERY_BROKER_URL=redis://memtest-redis:6379/0 -e CELERY_RESULT_BACKEND=redis://memtest-redis:6379/0 \\
      -e REDIS_BROKER_URI=redis://memtest-redis:6379/0 -e OIDC_ISSUER= <ap-explanation image>

Usage:
    python3 scripts/memtest_questions.py <meteo_queries.csv> zurich > assets/memory_sweep.jsonl
    python3 scripts/memtest_questions.py <meteo_queries.csv> 532 536 616
    python3 scripts/memtest_questions.py <meteo_queries.csv> 532 --semiring=why

``zurich`` runs every supported City of Zurich question the demo's data can answer: tables
tmin, tmax, tp, windspeedmax and elevation, years 2005-2019. No LLM is configured, so the
time is the provenance work only.
"""
import copy
import csv
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

SERVICE = "http://localhost:5011/api/v1/aps/explanation"
CONTAINER = "provsql-memtest"
DEADLINE_SECONDS = 180
LOADED = {"meteo_tmin", "meteo_tmax", "meteo_tp", "meteo_windspeedmax", "meteo_elevation"}
TEMPLATE = json.loads((Path(__file__).parent.parent / "assets" / "aps" / "G81.json").read_text())
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")


def ap_for(row: dict) -> dict:
    """The G81 AP with this question's query, and one Table node per table it reads."""
    ap = copy.deepcopy(TEMPLATE)
    table = next(n for n in ap["nodes"] if "Table" in n["labels"])
    edge = next(e for e in ap["edges"] if e["to"] == table["id"] and "containedIn" in e["labels"])
    ap["nodes"].remove(table)
    ap["edges"].remove(edge)
    for node in ap["nodes"]:
        if "Provenance_SQL_Operator" in node["labels"]:
            node["properties"]["query"] = row["sql"]
        if "Analytical_Pattern" in node["labels"]:
            node["properties"]["name"] = f"Meteo question {row['question_id']}"
    for name in row["tables"].split(";"):
        node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memtest/{row['question_id']}/{name}"))
        ap["nodes"].append({"id": node_id, "labels": ["Table"], "properties": {"name": f"meteo.{name}"}})
        ap["edges"].append({**edge, "to": node_id})
    return ap


def runnable_in_zurich(row: dict) -> bool:
    years = [int(y) for y in _YEAR_RE.findall(row["sql"])]
    return (row["supported"] == "true" and "Zurich" in row["question"]
            and set(row["tables"].split(";")) <= LOADED
            and (not years or (min(years) >= 2005 and max(years) <= 2019)))


def peak_backend_kb() -> int:
    """Largest peak memory (VmHWM) among the client backends on database meteo."""
    query = (r"SELECT coalesce(max(substring(pg_read_file('/proc/' || pid || '/status', true) "
             r"FROM 'VmHWM:\s+(\d+) kB')::bigint), 0) FROM pg_stat_activity "
             r"WHERE datname = 'meteo' AND backend_type = 'client backend'")
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "provdemo", "-d", "memtest", "-Atc", query],
        capture_output=True, text=True, check=False).stdout.strip()
    return int(out) if out.isdigit() else 0


def call(url: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"}, method="POST" if body else "GET")
    return json.load(urllib.request.urlopen(request, timeout=30))


def measure(row: dict, semiring: str | None = None) -> dict:
    """Without ``semiring``, every semiring is requested, as the demo does."""
    t0, peak = time.monotonic(), 0
    url = f"{SERVICE}/{semiring}" if semiring else SERVICE
    task_id = call(f"{url}?probability=false", ap_for(row))["task_id"]
    status = {"status": "timeout", "error": f"no result after {DEADLINE_SECONDS} s"}
    while time.monotonic() - t0 < DEADLINE_SECONDS:
        time.sleep(0.3)
        peak = max(peak, peak_backend_kb())
        polled = call(f"{SERVICE}/{task_id}")
        if polled["status"] not in ("pending", "started", "retry"):
            status = polled
            break
    derivations = (status.get("result") or {}).get("derivations") or []
    if status["status"] == "timeout":
        # ProvSQL ignores cancellation while evaluating: only a restart stops it.
        subprocess.run(["docker", "restart", CONTAINER], capture_output=True, check=False)
        time.sleep(15)
    returned = sorted({s for d in derivations for s in (d.get("provenance") or {})})
    return {"question_id": row["question_id"], "semiring": semiring or "all",
            "semirings_returned": returned, "status": status["status"],
            "seconds": round(time.monotonic() - t0, 1), "peak_backend_mb": round(peak / 1024),
            "rows": len(derivations), "error": str(status.get("error") or "")[:200]}


def main(csv_path: str, args: list[str]) -> None:
    semiring = next((a.split("=", 1)[1] for a in args if a.startswith("--semiring=")), None)
    targets = [a for a in args if not a.startswith("--")]
    with open(csv_path, newline="") as f:
        rows = {r["question_id"]: r for r in csv.DictReader(f)}
    ids = [q for q, r in rows.items() if runnable_in_zurich(r)] if targets == ["zurich"] else targets
    for question_id in ids:
        print(json.dumps(measure(rows[question_id], semiring)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
