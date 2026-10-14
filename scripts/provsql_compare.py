"""Compare ProvSQL versions on the meteo questions: the six demo questions and the City of
Zurich question of every unsupported template, run as ap-explanation would run them, with
their outcome, time, peak memory of the database backend, and whether the answer is the
plain SQL one.

Every query runs in a database of its own, started from an annotated snapshot and capped
at 3 GB: ProvSQL keeps the circuits of every query in a store shared by the whole database,
so a query that builds a huge one would change what the next queries measure.

Setup, one snapshot image per ProvSQL version:
    # ProvSQL master built on the 1.12 image (same PostgreSQL, same build tools)
    git clone https://github.com/PierreSenellart/provsql /tmp/provsql
    git -C /tmp/provsql checkout 45320ea7
    printf 'FROM ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0\\nCOPY . /provsql-master\\nRUN cd /provsql-master && make -j"$(nproc)" && make install\\n' \\
      | docker build -t provsql:master -f - /tmp/provsql
    # PGDATA outside the image's volume, so that docker commit keeps the database
    for v in ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0=1.12 provsql:master=1.13; do
      docker run -d --name prep -e PGDATA=/pgdata -e POSTGRES_USER=provdemo \\
        -e POSTGRES_PASSWORD=provdemo -e POSTGRES_DB=meteo ${v%=*}
      until docker logs prep 2>&1 | grep -q 'PostgreSQL init process complete'; do sleep 1; done
      until docker exec prep pg_isready -q -U provdemo -d meteo; do sleep 1; done
      docker cp dependencies/postgres-seed/. prep:/docker-entrypoint-initdb.d/
      docker exec prep psql -U provdemo -d meteo -f /docker-entrypoint-initdb.d/01_meteo.sql
      cat scripts/meteo_standins.sql scripts/provsql_compare_setup.sql | docker exec -i prep psql -U provdemo -d meteo
      docker stop prep && docker commit prep meteo-ready:${v#*=} && docker rm prep
    done

Usage, with a checkout of ap-explanation at v1.2.0 for its SQL rewriter (the refusals it lifts
are found by their text):
    python3 scripts/provsql_compare.py sweep meteo-ready:1.12 1.12 <ap-explanation> > assets/provsql_113_sweep.jsonl
    python3 scripts/provsql_compare.py sweep meteo-ready:1.13 1.13 <ap-explanation> >> assets/provsql_113_sweep.jsonl
    python3 scripts/provsql_compare.py sweep meteo-ready:1.13 1.13 <ap-explanation> --adapted >> assets/provsql_113_sweep.jsonl
    python3 scripts/provsql_compare.py checks meteo-ready:1.12 1.12 > assets/provsql_113_checks.jsonl
    python3 scripts/provsql_compare.py checks meteo-ready:1.13 1.13 >> assets/provsql_113_checks.jsonl

With ``--supported=assets/meteo_queries.csv`` (ap-explanation's fixtures/meteo_queries.csv,
with the SQL of every question), ``sweep`` runs the supported City of Zurich questions
instead, to look for regressions (assets/provsql_113_supported_adapted.jsonl), or the
questions whose ids follow, whatever their region (on the snapshot of
scripts/apx_service_check.py with --regions, assets/provsql_113_regions_adapted.jsonl).

With ``--max-memory=1GB``, ``sweep`` sets ``provsql.max_memory`` (ProvSQL 1.13) for every question.
``sweep`` lifts ap-explanation's own refusals (HAVING, aggregates outside its list) to see
what ProvSQL does with the query, and records the refusal. ``--adapted`` sends what
ap-explanation would have to send ProvSQL 1.13 to get the plain SQL answer back: every
``LIMIT k`` as ``LIMIT plain(k)``, and only the rows where ``present(provenance())``.
``checks`` runs the run-time problems one by one: comparison on an aggregate, top-k, large
aggregates, cancellation, memory limit.

The script reaches each container at its address on the Docker bridge. Set CONTAINER to
run several at once.
"""
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import psycopg
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).parent.parent
CONTAINER = os.environ.get("CONTAINER", "provsql-compare")
SEMIRING = "formula"
TIMEOUT_SECONDS = 120
_KILLED_RE = re.compile(r'(server process|background worker "[^"]+") \(PID \d+\) was terminated by signal (\d+)')


def load_rewriters(checkout: str) -> tuple[type, type]:
    """ap-explanation's SqlRewriter as it is, and with its own refusals lifted."""
    for name in ("ap_explanation", "ap_explanation.errors", "ap_explanation.types",
                 "ap_explanation.types.semiring"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["ap_explanation.errors"].AggregateSemiringError = type("AggregateSemiringError", (Exception,), {})
    sys.modules["ap_explanation.types.semiring"].DbSemiring = object
    source = (Path(checkout) / "ap_explanation/internal/sql_rewriter.py").read_text()
    lifted = (source.replace("if root.find(Having):", "if False:")
              .replace("if root.find(Except, Intersect):", "if False:")
              .replace("and all(isinstance(a, self.PROVSQL_AGGREGATES) for a in aggregates)", ""))
    if lifted.count("if False:") != 2 or len(lifted) >= len(source):
        raise SystemExit("ap-explanation's sql_rewriter.py changed: its refusals were not found")
    strict_ns, lifted_ns = {}, {}
    exec(compile(source, "sql_rewriter.py", "exec"), strict_ns)  # noqa: S102
    exec(compile(lifted, "sql_rewriter.py", "exec"), lifted_ns)  # noqa: S102
    return strict_ns["SqlRewriter"], lifted_ns["SqlRewriter"]


def formula_semiring() -> types.SimpleNamespace:
    return types.SimpleNamespace(name=SEMIRING, retrieval_function=f"sr_{SEMIRING}",
                                 mapping_table=f"{SEMIRING}_mapping", supports_aggregates=True)


def with_plain_limits(sql: str) -> str:
    root = sqlglot.parse_one(sql, dialect="postgres")
    for limit in root.find_all(exp.Limit):
        limit.set("expression", exp.Anonymous(this="plain", expressions=[limit.expression]))
    return root.sql(dialect="postgres")


def rewrite(sql: str, rewriters: tuple[type, type], adapted: bool) -> tuple[str, str]:
    """The query to run, and ap-explanation's refusal of the original one, if any."""
    strict, lifted = rewriters
    refusal = ""
    try:
        strict().rewrite(sql, [formula_semiring()])
    except Exception as e:  # noqa: BLE001
        refusal = f"{type(e).__name__}: {e}"[:160]
    if not adapted:
        return lifted().rewrite(sql, [formula_semiring()]), refusal
    rewritten = lifted().rewrite(with_plain_limits(sql), [formula_semiring()])
    if rewritten.rstrip().endswith("AS x"):  # an aggregate, already wrapped as a subquery
        return rewritten + " WHERE present(provenance())", refusal
    return (f"SELECT y.*, sr_{SEMIRING}(provenance(), '{SEMIRING}_mapping') "
            f"FROM ({with_plain_limits(sql)}) AS y WHERE present(provenance())"), refusal


def questions(supported_csv: str | None, only: set[str] = frozenset()) -> list[tuple[str, str]]:
    """The demo questions and the unsupported City of Zurich ones or, from the question
    set given with its SQL, the supported City of Zurich ones (any question, when ids are
    given)."""
    if supported_csv:
        with open(supported_csv, newline="") as f:
            return [(r["question_id"], r["sql"]) for r in csv.DictReader(f)
                    if r["question_id"] in only
                    or not only and r["supported"] == "true" and "Zurich" in r["question"] and r["sql"]]
    out = []
    for path in sorted((ROOT / "assets/aps").glob("*.json")):
        for node in json.loads(path.read_text())["nodes"]:
            if "Provenance_SQL_Operator" in node.get("labels", []):
                out.append((path.stem, node["properties"]["query"]))
    with (ROOT / "assets/meteo_queries.csv").open(newline="") as f:
        out += [(r["question_id"], r["sql"]) for r in csv.DictReader(f)
                if r["supported"] != "true" and "Zurich" in r["question"]]
    return out


def connect(host: str) -> psycopg.Connection:
    conn = psycopg.connect(host=host, user="provdemo", password="provdemo", dbname="meteo",
                           autocommit=True, connect_timeout=5)
    conn.execute("SET search_path TO meteo, public, provsql")
    return conn


def fresh(image: str, memory: str = "3g") -> str:
    """A new database from the snapshot *image*, capped at *memory*; returns its address."""
    subprocess.run(["docker", "rm", "-f", "-v", CONTAINER], capture_output=True, check=False)
    subprocess.run(["docker", "run", "-d", "--name", CONTAINER, f"--memory={memory}",
                    f"--memory-swap={memory}", image], capture_output=True, check=True)
    for _ in range(600):
        host = subprocess.run(
            ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", CONTAINER],
            capture_output=True, text=True, check=False).stdout.strip()
        try:
            conn = connect(host)
            # The image's entrypoint upgrades the extension once the server accepts connections.
            upgrading = conn.execute(
                "SELECT installed_version <> default_version FROM pg_available_extensions "
                "WHERE name = 'provsql'").fetchone()[0]
            conn.close()
            if not upgrading:
                return host
        except psycopg.Error:
            pass
        time.sleep(0.5)
    raise SystemExit("the database did not start")


def store_mb() -> int | None:
    """Size on disk of ProvSQL's circuit store."""
    out = subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", "du -cm /pgdata/base/*/provsql_*.mmap | tail -1"],
                         capture_output=True, text=True, check=False).stdout.split()
    return int(out[0]) if out else None


def processes_lost() -> list[str]:
    """The server processes the kernel killed, from the server log."""
    logs = subprocess.run(["docker", "logs", CONTAINER], capture_output=True, text=True, check=False)
    return [f"{who}, signal {sig}" for who, sig in _KILLED_RE.findall(logs.stdout + logs.stderr)]


def run_tracked(host: str, sql: str, timeout: int = TIMEOUT_SECONDS, settings: tuple[str, ...] = ()) -> dict:
    """Run *sql* with ProvSQL on, sampling the backend's private memory every 0.1 s."""
    out = {"status": "success", "sqlstate": "", "error": "", "warnings": []}
    conn = connect(host)
    conn.add_notice_handler(lambda d: d.severity == "WARNING" and out["warnings"].append(
        (d.message_primary or "").removeprefix("ProvSQL: ")[:160]))
    pid = conn.info.backend_pid
    for setting in ("SET provsql.active = 1", f"SET statement_timeout = '{timeout}s'", *settings):
        conn.execute(setting)
    peak, done, box = [0], threading.Event(), {}

    def sample() -> None:
        monitor = connect(host)
        while not done.is_set():
            try:
                kb = monitor.execute(
                    r"SELECT substring(pg_read_file('/proc/' || %s || '/status', true) FROM 'RssAnon:\s+(\d+) kB')",
                    (pid,)).fetchone()[0]
            except psycopg.Error:
                return
            peak[0] = max(peak[0], int(kb or 0))
            time.sleep(0.1)

    def work() -> None:
        try:
            cursor = conn.execute(sql)
            box["columns"] = [c.name for c in cursor.description]
            box["rows"] = cursor.fetchall()
        except psycopg.Error as e:
            box["error"] = e

    threading.Thread(target=sample, daemon=True).start()
    t0 = time.monotonic()
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout + 45)
    if worker.is_alive():  # the statement ignored statement_timeout: only a restart stops it
        subprocess.run(["docker", "restart", "-t", "2", CONTAINER], capture_output=True, check=False)
        worker.join(30)
        box = {"hung": True}
    out["seconds"] = round(time.monotonic() - t0, 2)
    done.set()
    out["peak_backend_mb"] = round(peak[0] / 1024)
    if box.get("hung"):
        out.update(status="timeout", error=f"ignored statement_timeout, restarted after {out['seconds']} s")
    elif "error" in box:
        e = box["error"]
        out.update(status="timeout" if e.sqlstate == "57014" else "failure", sqlstate=e.sqlstate or "",
                   error=" ".join((e.diag.message_primary or str(e)).split())[:240])
    else:
        out["columns"], out["rows"] = box["columns"], box["rows"]
    out["warnings"] = sorted(set(out["warnings"]))
    if out["status"] != "success":
        time.sleep(2)  # leave the server the time to log what it lost
    out["lost"] = processes_lost()
    return out


def normalised(rows: list) -> list:
    def cell(value: object) -> str:
        text = re.sub(r" \(\*\)$", "", str(value))  # an aggregate prints as '13 (*)'
        try:
            return f"{float(text):.9g}"
        except ValueError:
            return text
    return sorted(tuple(cell(v) for v in row) for row in rows)


def sweep(image: str, label: str, checkout: str, args: list[str]) -> None:
    adapted = "--adapted" in args
    supported_csv = next((a.split("=", 1)[1] for a in args if a.startswith("--supported=")), None)
    max_memory = next((a.split("=", 1)[1] for a in args if a.startswith("--max-memory=")), None)
    settings = (f"SET provsql.max_memory = '{max_memory}'",) if max_memory else ()
    only = {a for a in args if not a.startswith("--")}
    rewriters = load_rewriters(checkout)
    for question_id, sql in questions(supported_csv, only):
        if only and question_id not in only:
            continue
        host = fresh(image)
        record = {"run": label, "adapted": adapted, "question_id": question_id}
        plain = connect(host)
        plain.execute("SET provsql.active = 0")
        try:
            plain_rows = plain.execute(sql).fetchall()
        except psycopg.Error as e:  # a table or a function the snapshot lacks
            print(json.dumps(record | {"status": "failure", "sqlstate": e.sqlstate or "",
                                       "error": f"plain SQL: {' '.join(str(e).split())[:200]}"}), flush=True)
            continue
        finally:
            plain.close()
        rewritten, record["apx_refusal"] = rewrite(sql, rewriters, adapted)
        store = store_mb()
        result = run_tracked(host, rewritten, settings=settings)
        rows, columns = result.pop("rows", None), result.pop("columns", None)
        record |= {"max_memory": max_memory} | result | {"plain_rows": len(plain_rows), "store_growth_mb": (store_mb() or 0) - (store or 0)}
        if rows is not None:
            function = f"sr_{SEMIRING}"
            formulas = [str(r[columns.index(function)]) for r in rows]
            answer = [i for i, c in enumerate(columns) if c not in (function, "provsql")]
            record |= {
                "rows": len(rows),
                "matches_plain": normalised([[r[i] for i in answer] for r in rows]) == normalised(plain_rows),
                "formula_chars": sum(map(len, formulas)),
                "formula_refs": sum(len(re.findall(r"@k[0-9a-f]{32}", f)) for f in formulas),
            }
        print(json.dumps(record), flush=True)
        print(question_id, record["status"], record.get("matches_plain"), record["error"][:80], file=sys.stderr)


_MONTHS_OVER = ("SELECT x.m, sr_formula(provenance(), 'formula_mapping') FROM (SELECT EXTRACT(MONTH FROM time) AS m, "
                "SUM(tp) AS s FROM meteo_tp WHERE EXTRACT(YEAR FROM time) = 2019 GROUP BY 1) x WHERE s > 0.001")
_TOP_10 = ("SELECT tmin, time, sr_formula(provenance(), 'formula_mapping') FROM meteo_tmin "
           "WHERE EXTRACT(YEAR FROM time) = 2015 AND EXTRACT(MONTH FROM time) = 7 ORDER BY tmin LIMIT 10")
_ALL_POINTS = "SELECT x.*, sr_formula(x.s, 'formula_mapping') FROM (SELECT SUM(elevation) AS s FROM meteo_elevation) AS x"
_MONTH_JOINED = (
    "SELECT x.*, sr_formula(x.s, 'formula_mapping') FROM (SELECT MAX(t.tp) AS s FROM meteo_tp t JOIN meteo_elevation e "
    "ON ROUND(CAST(t.latitude AS DECIMAL), 1) = ROUND(CAST(e.latitude AS DECIMAL), 1) "
    "WHERE EXTRACT(YEAR FROM t.time) = 2019 AND EXTRACT(MONTH FROM t.time) = 1 AND e.elevation > 500) AS x")
_EMPTY = ("CREATE TABLE empty_tmax (latitude real, longitude real, time timestamp, tmax double precision)",
          "SELECT add_provenance('empty_tmax')", "SELECT create_provenance_mapping('empty_map', 'empty_tmax', 'time')")
_READINGS_23 = ("CREATE TABLE t AS SELECT 1 AS g, i AS id, (1 + (i % 9))::float8 / 1000 AS v FROM generate_series(1, 23) i",
                "SELECT add_provenance('t')", "SELECT create_provenance_mapping('t_map', 't', 'id')")
# The 8 lowest points of the cell, the lowest one stored last.
_POINTS_8 = ("SET provsql.active = 0",
             "CREATE TABLE pts AS SELECT * FROM (SELECT latitude, longitude, elevation FROM meteo_elevation "
             "ORDER BY elevation LIMIT 8) s ORDER BY elevation DESC",
             "SET provsql.active = 1", "SELECT add_provenance('pts')")
_LOWEST_POINT = "SELECT elevation FROM pts WHERE elevation = (SELECT MIN(elevation) FROM pts) LIMIT 1"

# (name, what, setup, query, settings of ProvSQL 1.13, statement_timeout, memory cap)
CHECKS = [
    ("comparison", "Comparison on a SUM: months of 2019 with more than 1 mm (Q295's shape, 28-31 readings per month)",
     (), _MONTHS_OVER, (), 60, "3g"),
    ("comparison_unlimited", "Same, without the world limit and with provsql.max_memory = 1GB",
     (), _MONTHS_OVER, ("SET provsql.max_worlds = 0", "SET provsql.max_memory = '1GB'"), 60, "3g"),
    ("top_k", "ORDER BY ... LIMIT 10 over 31 readings (G81's shape)", (), _TOP_10, (), 60, "3g"),
    ("top_k_max_memory", "Same, with provsql.max_memory = 1GB", (), _TOP_10, ("SET provsql.max_memory = '1GB'",), 60, "3g"),
    ("aggregate", "SUM over the 134,280 elevation points", (), _ALL_POINTS, (), 120, "3g"),
    ("join", "MAX over one month of readings joined to the elevation points above 500 m", (), _MONTH_JOINED, (), 240, "3g"),
    ("join_10g", "Same, in a database capped at 10 GB", (), _MONTH_JOINED, (), 240, "10g"),
    ("count_distinct_empty", "COUNT(DISTINCT ...) on an empty table", _EMPTY,
     "SELECT x.*, sr_formula(x.c, 'empty_map') FROM (SELECT COUNT(DISTINCT DATE(time)) AS c FROM empty_tmax) AS x",
     (), 30, "3g"),
    ("cancel", "statement_timeout of 3 s on an evaluation of about 11 s (sr_how, SUM > c over 23 readings)", _READINGS_23,
     "SELECT length(sr_how(provenance(), 't_map')) FROM (SELECT g, SUM(v) AS s FROM t GROUP BY g) x WHERE s > 0.001",
     ("SET provsql.max_worlds = 0",), 3, "3g"),
    ("lowest_point", "WHERE elevation = (SELECT MIN(elevation) ...) LIMIT 1 over 8 points", _POINTS_8,
     _LOWEST_POINT, (), 60, "3g"),
]


def checks(image: str, label: str, args: list[str]) -> None:
    for name, what, setup, sql, new_settings, timeout, memory in CHECKS:
        if args and name not in args:
            continue
        host = fresh(image, memory)
        conn = connect(host)
        is_new = conn.execute("SELECT count(*) FROM pg_settings WHERE name = 'provsql.max_worlds'").fetchone()[0] > 0
        if new_settings and not is_new and name != "cancel":
            continue  # the same check with settings ProvSQL 1.12 does not have
        conn.execute("SET provsql.active = 1")
        for statement in setup:
            conn.execute(statement)
        record = {"run": label, "check": name, "what": what, "memory_cap": memory,
                  "settings": [s.removeprefix("SET ") for s in new_settings] if is_new else [],
                  "statement_timeout": timeout}
        if name == "lowest_point":
            conn.execute("SET provsql.active = 0")
            record["plain_answer"] = str(conn.execute(sql).fetchone()[0])
        conn.close()
        result = run_tracked(host, sql, timeout, new_settings if is_new else ())
        rows = result.pop("rows", None)
        result.pop("columns", None)
        record |= result | {"store_mb": store_mb(),
                            "output_chars": sum(len(str(c)) for r in rows or [] for c in r)}
        if name == "lowest_point" and rows:
            record["answer"] = str(rows[0][0])
        print(json.dumps(record), flush=True)
        print(name, record["status"], record["seconds"], record["peak_backend_mb"], record["error"][:80], file=sys.stderr)


if __name__ == "__main__":
    command, image, label, *rest = sys.argv[1:]
    if command == "sweep":
        sweep(image, label, rest[0], rest[1:])
    else:
        checks(image, label, rest)
    subprocess.run(["docker", "rm", "-f", "-v", CONTAINER], capture_output=True, check=False)
