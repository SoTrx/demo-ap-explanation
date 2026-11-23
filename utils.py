import copy
import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"
APS_DIR = ASSETS_DIR / "aps"
RECORDED_DIR = ASSETS_DIR / "recorded"
PRESETS_PATH = Path(__file__).parent / "presets" / "meteo_presets.json"
# The meteo question set, classified by ap-explanation's scripts/classify_meteo_queries.py
# (fixtures/meteo_queries.csv there, copied with the SQL of every question).
QUESTION_SET_PATH = ASSETS_DIR / "meteo_queries.csv"

# Why a question is unsupported: (pattern of the error the classifier recorded, short
# name, explanation). The first matching pattern wins.
UNSUPPORTED_REASONS = [
    ("could not find CTE", "Scalar subquery reading a CTE", (
        "A CTE compares a column to a scalar subquery that reads another CTE, e.g. "
        "`elevation = (SELECT min_elev FROM min_elev_value)`. The provenance rewriting of that "
        "subquery loses the CTE, and PostgreSQL fails with `could not find CTE`."
    )),
    ("HAVING", "HAVING clause", (
        "ap-explanation's rewriter rejects `HAVING`. The same filter written as a `WHERE` on a "
        "nested `SELECT` is accepted, but it then compares a nested aggregate, which can "
        "exhaust memory (see *Memory analysis*)."
    )),
    ("ORDER BY on", "ORDER BY on an aggregate", (
        "The query sorts groups by an aggregate, e.g. `ORDER BY AVG(tp) DESC LIMIT 1` to pick the "
        "wettest month. ProvSQL turns aggregate values into provenance tokens, which it cannot sort."
    )),
    ("Aggregates ProvSQL cannot explain", "Aggregate ProvSQL cannot explain", (
        "`PERCENTILE_CONT`, `REGR_SLOPE` or `CORR`. ProvSQL explains `COUNT`, `SUM`, `MIN`, `MAX`, "
        "`AVG`, `ARRAY_AGG`, `BOOL_AND` and `BOOL_OR` only."
    )),
    ("window functions", "Window function", (
        "`LAG(...) OVER (...)` and the like. ProvSQL tracks provenance per input row only and treats "
        "the windowed value as opaque, so the provenance would be incomplete."
    )),
    ("Subqueries", "Subquery in a condition", (
        "`WHERE tmin = (SELECT MIN(tmin) ...)`, `EXISTS` or `IN (SELECT ...)`: ProvSQL does not "
        "support subqueries inside expressions."
    )),
    ("invalid input syntax for type uuid", "COUNT(DISTINCT …) without GROUP BY", (
        "A ProvSQL 1.12 bug on an empty table (the classification runs on empty tables): these "
        "questions may work on real data."
    )),
]

# One input row in a ProvSQL formula: ``<table>@k<32 hex>``.
_REFERENCE_RE = re.compile(r"\b(\w+)@k[0-9a-f]{32}\b")

# Per measure column: its unit in the database (ERA5) and how the demo displays it.
_UNITS = {
    "tmin": lambda v: f"{v - 273.15:.1f} °C ({v:.2f} K)",
    "tmax": lambda v: f"{v - 273.15:.1f} °C ({v:.2f} K)",
    "tp": lambda v: f"{v * 1000:.1f} mm",
    "windspeedmax": lambda v: f"{v:.1f} m/s",
    "elevation": lambda v: f"{v:.1f} m",
}


def load_presets() -> list[dict]:
    return json.loads(PRESETS_PATH.read_text())


def load_ap_json(file_path: str | Path) -> dict:
    with open(file_path) as f:
        return json.load(f)


def load_ap(ap_id: str) -> dict:
    return load_ap_json(APS_DIR / f"{ap_id}.json")


def load_recorded(ap_id: str) -> dict | None:
    """The response ap-explanation gave on this AP, saved by its
    ``scripts/meteo_examples/run_aps.py``: ``{plain, status, seconds, error, result}``."""
    path = RECORDED_DIR / f"{ap_id}.response.json"
    return load_ap_json(path) if path.exists() else None


def stamp_start_time(ap_data: dict) -> dict:
    """A copy of the AP with its ``startTime`` set to now. ap-explanation caches results
    (1 h) under a key made of the whole AP, so this makes every live run compute afresh,
    and its time and memory reflect the actual work."""
    ap_data = copy.deepcopy(ap_data)
    for node in _nodes_with(ap_data, "Analytical_Pattern"):
        node.setdefault("properties", {})["startTime"] = datetime.now(UTC).strftime("%H:%M:%S")
    return ap_data


def _nodes_with(ap_data: dict, label: str) -> list[dict]:
    return [n for n in ap_data.get("nodes", []) if label in n.get("labels", [])]


def extract_sql(ap_data: dict) -> str | None:
    """The query of the ``Provenance_SQL_Operator`` node."""
    for node in _nodes_with(ap_data, "Provenance_SQL_Operator"):
        return (node.get("properties") or {}).get("query")
    return None


def database_name(ap_data: dict) -> str | None:
    """The ``RelationalDatabase`` node name: ap-explanation uses it as both the
    database and the schema to run the query in."""
    for node in _nodes_with(ap_data, "RelationalDatabase"):
        return (node.get("properties") or {}).get("name")
    return None


def ap_tables(ap_data: dict) -> list[str]:
    return [(n.get("properties") or {}).get("name", "?") for n in _nodes_with(ap_data, "Table")]


def cited_readings(derivation: dict, semiring: str = "formula") -> list[dict]:
    """The input rows a derivation's provenance cites in ``semiring``, once each
    (the service returns a row once per occurrence in the expression)."""
    data = ((derivation.get("provenance") or {}).get(semiring) or {}).get("data") or []
    seen: dict[str, dict] = {}
    for item in data:
        ref = item.get("reference")
        if ref and ref not in seen:
            seen[ref] = item.get("data") or {}
    return [{"reference": ref, "data": row} for ref, row in seen.items()]


def reading_labels(readings: list[dict]) -> dict[str, str]:
    """A short, stable name per cited row (``tmin#3`` for ``meteo_tmin``), numbered
    per table in citation order, to read the formula against the readings table."""
    labels: dict[str, str] = {}
    counts: dict[str, int] = {}
    for r in readings:
        table = r["reference"].split("@", 1)[0].removeprefix("meteo_")
        counts[table] = counts.get(table, 0) + 1
        labels[r["reference"]] = f"{table}#{counts[table]}"
    return labels


def shorten_formula(expression: str, labels: dict[str, str]) -> str:
    """Replace each ``<table>@k<hex>`` token with its reading label."""
    return _REFERENCE_RE.sub(lambda m: labels.get(m.group(0), m.group(1)), expression)


def _format_time(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%a %d %b %Y")
    except ValueError:
        return str(value)


def format_reading(label: str, row: dict) -> dict:
    """One cited row for display: its label, date, place and measure with units."""
    out = {"Reading": label, "Date": _format_time(row["time"]) if "time" in row else "—"}
    out["Place"] = f"{row.get('latitude')}, {row.get('longitude')}"
    measures = [f"{col}: {fmt(row[col])}" for col, fmt in _UNITS.items()
                if isinstance(row.get(col), (int, float))]
    out["Value"] = " · ".join(measures) or json.dumps(row, default=str)
    return out


def load_question_set() -> list[dict]:
    with QUESTION_SET_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def unsupported_reason(error: str) -> tuple[str, str]:
    """``(short name, explanation)`` of the classifier's recorded error."""
    for pattern, name, why in UNSUPPORTED_REASONS:
        if pattern in error:
            return name, why
    return "Other", error


# Which questions can make ProvSQL exhaust the database's memory: assets/memory_risk.csv
# comes from scripts/memory_analysis.py, assets/memory_sweep.jsonl from
# scripts/memtest_questions.py (ProvSQL 1.12).
MEMORY_RISK_PATH = ASSETS_DIR / "memory_risk.csv"
MEMORY_SWEEP_PATH = ASSETS_DIR / "memory_sweep.jsonl"


def load_memory_risk() -> list[dict]:
    with MEMORY_RISK_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["estimated_mb"] = float(r["estimated_mb"])
    return rows


def load_memory_sweep() -> dict[str, dict]:
    """Measured runs by question id: ``{status, seconds, peak_backend_mb, rows, error}``."""
    with MEMORY_SWEEP_PATH.open() as f:
        runs = [json.loads(line) for line in f if line.strip()]
    return {r["question_id"]: r for r in runs}


def format_mb(mb: float) -> str:
    if mb == float("inf"):
        return "unbounded"
    for unit, size in (("PB", 1024**3), ("TB", 1024**2), ("GB", 1024)):
        if mb >= size:
            value = mb / size
            return f"{value:,.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{mb:.0f} MB"


# Status of a question in the Summary tab, worst first.
STATUS_UNSUPPORTED = "Not supported"
STATUS_CAVEAT = "Supported, with caveats"  # stopped for lack of memory or time, or no row in the sample
STATUS_OK = "Supported"
STATUS_CRASH = "Runtime crash"


def question_statuses() -> list[dict]:
    """Every question of the set with its status and why:

    - Not supported: ap-explanation rejects it, or it fails at run time for another
      reason than memory (a failure measured on one question of a template is assumed
      for the others: their SQL differs only by the region);
    - Runtime crash: it exhausts the memory and nothing stops it (all those estimated above
      3 GB on ProvSQL 1.12, which has no budget);
    - Supported, with caveats: it runs, but the demo's data has no row for it;
    - Supported: the rest, with the estimate when it is above 100 MB.

    A supported template whose City of Zurich question, sent to ap-explanation, does not
    return the plain SQL answer is Not supported.
    """
    questions = load_question_set()
    risk: dict[str, list[dict]] = {}
    for r in load_memory_risk():
        risk.setdefault(r["question_id"], []).append(r)
    sweep = load_memory_sweep()
    template_of = {q["question_id"]: (q["category"], q["template_index"]) for q in questions}
    # Sent to ap-explanation, the City of Zurich question of the template does not return
    # the plain SQL answer (scripts/apx_service_check.py).
    wrong_templates = {template_of[q]: (q, r) for q, r in load_service_runs("1.12").items()
                       if service_outcome(r) == "wrong"}
    # The database was killed on the City of Zurich question of the template (measured through
    # ap-explanation, or run alone): its other questions are assumed to take it down too.
    crashed_templates = {template_of[q]: q for q, r in load_service_runs("1.12").items() if r["lost"]}
    crashed_templates |= {template_of[r["question_id"]]: r["question_id"] for r in sweep.values()
                          if r["status"] == "failure" and "server closed the connection" in r["error"]
                          and r["question_id"] in template_of}
    empty_templates = {template_of[q] for q, r in load_service_runs("1.12").items()
                       if service_outcome(r) == "empty"}
    failing_templates = {}
    for run in sweep.values():
        if (run["status"] == "failure" and "server closed the connection" not in run["error"]
                and run["question_id"] in template_of):
            failing_templates[template_of[run["question_id"]]] = (run["question_id"], run["error"])

    out = []
    for q in questions:
        findings = sorted(risk.get(q["question_id"], []), key=lambda r: -r["estimated_mb"])
        worst = findings[0] if findings else None
        failure = failing_templates.get((q["category"], q["template_index"]))
        estimate = format_mb(worst["estimated_mb"]) if worst else ""
        if q["supported"] != "true":
            status, why = STATUS_UNSUPPORTED, unsupported_reason(q["reason"])[0]
        elif failure:
            measured_on, error = failure
            status = STATUS_UNSUPPORTED
            why = f"Fails at run time (measured on {measured_on}): {error[:90]}"
        elif (q["category"], q["template_index"]) in wrong_templates:
            measured_on, run = wrong_templates[(q["category"], q["template_index"])]
            status = STATUS_UNSUPPORTED
            why = (f"Not the SQL answer (measured on {measured_on} through ap-explanation): "
                   f"{run['rows']} rows, values that differ from the plain SQL ones")
        elif worst and worst["verdict"] in ("exhausts", "unknown"):
            # ProvSQL 1.12 has no memory budget: nothing stops these, they exhaust the memory
            status = STATUS_CRASH
            crashed = crashed_templates.get((q["category"], q["template_index"]))
            why = (f"Database killed at 3 GB (measured on {crashed}, same template): {worst['size_text']}"
                   if crashed else
                   f"Estimated to exhaust memory ({worst['size_text']}); ProvSQL 1.12 has no memory "
                   "limit to stop it")
        elif (q["category"], q["template_index"]) in empty_templates:
            status = STATUS_CAVEAT
            why = _SERVICE_WHY["empty"]
        else:
            status = STATUS_OK
            why = worst["size_text"] if worst and worst["verdict"] == "heavy" else ""
        run = sweep.get(q["question_id"])
        out.append({
            "Id": int(q["question_id"]), "Category": q["category"], "Question": q["question"],
            "Tables": q["tables"].replace(";", ", "), "Status": status, "Why": why,
            "Estimated memory": estimate if status != STATUS_UNSUPPORTED or worst and worst["verdict"] in ("exhausts", "unknown") else "",
            "Measured (3 GB cap)": (f"{format_mb(run['peak_backend_mb'])}, {run['status']}, {run['seconds']} s"
                                    if run else ""),
        })
    return out


# ProvSQL 1.13: the questions run on ProvSQL 1.12 and on the released 1.13.0. assets/provsql_113_*.jsonl come from scripts/provsql_compare.py.
PROVSQL_NEXT_COMMIT = "4488a39e"
PROVSQL_NEXT_BUILD = f"1.13.0 ({PROVSQL_NEXT_COMMIT}), released 17 October 2026"
# ProvSQL 1.14, not released yet: its master branch, with the fixes of issue #104 for the
# memory of loading a circuit.
PROVSQL_MASTER_COMMIT = "3b8d3098"
PROVSQL_MASTER_BUILD = f"master at {PROVSQL_MASTER_COMMIT}, 26 October 2026, 1.14.0-dev"
# ProvSQL 1.14.0, released 17 November 2026: master@3b8d3098 (in effect 1.13.1), and the
# probability evaluation splitting independent parts and caching compiled d-DNNFs
PROVSQL_114_BUILD = "1.14.0, released 17 November 2026"
PROVSQL_NEXT_SWEEP_PATH = ASSETS_DIR / "provsql_113_sweep.jsonl"
PROVSQL_NEXT_MAXMEM_SWEEP_PATH = ASSETS_DIR / "provsql_113_sweep_maxmem1g.jsonl"
PROVSQL_NEXT_CHECKS_PATH = ASSETS_DIR / "provsql_113_checks.jsonl"

# The runs of the comparison on ProvSQL 1.13, as (ProvSQL version, queries adapted to 1.13).
RUN_NEXT = ("1.13", False)
RUN_NEXT_ADAPTED = ("1.13", True)


def load_provsql_sweep() -> dict[tuple[str, bool], dict[str, dict]]:
    """Runs of scripts/provsql_compare.py, by ``(ProvSQL version, adapted)`` then question id."""
    runs: dict[tuple[str, bool], dict[str, dict]] = {}
    with PROVSQL_NEXT_SWEEP_PATH.open() as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                runs.setdefault((r["run"], r["adapted"]), {})[r["question_id"]] = r
    return runs


def load_provsql_sweep_max_memory() -> dict[str, dict]:
    """The questions run on ProvSQL 1.13 as sent today, with ``provsql.max_memory = '1GB'``
    (``scripts/provsql_compare.py sweep --max-memory=1GB``), by question id."""
    with PROVSQL_NEXT_MAXMEM_SWEEP_PATH.open() as f:
        return {r["question_id"]: r for r in map(json.loads, filter(str.strip, f))}


def load_provsql_checks() -> dict[str, dict[str, dict]]:
    """Run-time checks of scripts/provsql_compare.py, by check name then ProvSQL version,
    in the order ProvSQL 1.13 ran them: it has a few checks of its own settings."""
    with PROVSQL_NEXT_CHECKS_PATH.open() as f:
        records = [json.loads(line) for line in f if line.strip()]
    checks: dict[str, dict[str, dict]] = {r["check"]: {} for r in records if r["run"] == "1.13"}
    for r in records:
        checks.setdefault(r["check"], {})[r["run"]] = r
    return checks


# ap-explanation itself (scripts/apx_service_check.py), on the City of Zurich question of
# every template: v1.2.0 on ProvSQL 1.12, and master, adapted to ProvSQL 1.13 (LIMIT plain(k),
# present(provenance()), HAVING), on ProvSQL 1.13. The questions that may exhaust memory ran
# on ProvSQL 1.13 themselves, on the demo's data copied to every cell of their region and
# with provsql.max_memory = '1GB': through v1.2.0, and through the branch master merged for
# the 36 that v1.2.0 does not support (33 of them for their HAVING).
APX_SERVICE_PATHS = {"1.12": ASSETS_DIR / "apx_service_112.jsonl",
                     "1.14": ASSETS_DIR / "apx_service_114_master.jsonl",
                     "1.14.0": ASSETS_DIR / "apx_service_1140.jsonl",
                     "1.13": ASSETS_DIR / "apx_service_113_master.jsonl"}
APX_SERVICE_MEMORY_PATH = ASSETS_DIR / "apx_service_113_memory.jsonl"
# The questions of Attica among them, run again on ProvSQL 1.14 on the demo's data copied to
# Attica's 24 cells (scripts/apx_service_check.py prepare ... --regions=Attica)
APX_SERVICE_ATTICA_114_PATH = ASSETS_DIR / "apx_service_114_attica.jsonl"
APX_SERVICE_ATTICA_PATHS = {"1.14": APX_SERVICE_ATTICA_114_PATH,
                            "1.14.0": ASSETS_DIR / "apx_service_1140_attica.jsonl"}
APX_MASTER_COMMIT = "a6cf3ea"
# ap-explanation on ProvSQL 1.14: master with the aggregated-rows pre-check merged, and
# sr_formula_rows for aggregates (branch feat/formula-rows)
APX_FORMULA_ROWS_COMMIT = "bbb56a6"
# ap-explanation on ProvSQL 1.14.0: master requiring it, the 2.0.0 to be
APX_114_COMMIT = "9154dba"
SERVICE_DEADLINE_SECONDS = 180


def _jsonl_by_question(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["question_id"]: r for r in map(json.loads, filter(str.strip, f))}


def load_service_runs(version: str) -> dict[str, dict]:
    return _jsonl_by_question(APX_SERVICE_PATHS[version])


def load_service_memory_runs(version: str = "1.13") -> dict[str, dict]:
    """The questions that may exhaust memory, on their region's data: on ProvSQL 1.13.0, and for
    1.14 and 1.14.0 their runs of the Attica questions in place of theirs."""
    runs = _jsonl_by_question(APX_SERVICE_MEMORY_PATH)
    return runs | _jsonl_by_question(APX_SERVICE_ATTICA_PATHS[version]) if version in APX_SERVICE_ATTICA_PATHS else runs


def service_outcome(run: dict) -> str:
    """What became of a question sent to ap-explanation: ``ok`` (the plain SQL answer),
    ``empty`` (the same, with no row on the demo's data, so no provenance seen), ``wrong`` (not
    the plain SQL answer), ``unchecked`` (the plain SQL answer could not be computed), ``apx``
    (refused by ap-explanation), ``refused`` or ``error`` (by ProvSQL), ``limit`` (refused by
    provsql.max_worlds), ``stopped`` (by provsql.max_memory), ``disk`` (by temp_file_limit),
    ``rows`` (refused by ap-explanation's max_aggregated_rows before it runs), ``slow`` (its plain
    SQL alone too slow for that check), ``timeout`` or ``killed``."""
    error = run["error"]
    if run["lost"]:
        return "killed"
    if "plain query's statement_timeout" in error:
        return "slow"
    if run["status"] == "timeout" or "statement timeout" in error or "statement_timeout" in error:
        return "timeout"
    if run["status"] != "success":
        if error.startswith(("HAVING queries are not supported", "Aggregates ProvSQL cannot explain")):
            return "apx"
        if "max_aggregated_rows" in error:
            return "rows"
        if "max_memory" in error:
            return "stopped"
        if "temp_file_limit" in error:
            return "disk"
        if "max_worlds" in error:
            return "limit"
        return "refused" if error.startswith("ProvSQL:") else "error"
    if run.get("matches_plain") is None:
        return "unchecked"
    if not run["matches_plain"]:
        return "wrong"
    return "ok" if run["plain_rows"] else "empty"


_SERVICE_WHY = {
    "apx": "Refused by ap-explanation",
    "empty": "Supported, but the demo's data has no row for it: its provenance was not seen",
    "wrong": "Not the SQL answer",
    "limit": "Refused by ProvSQL (provsql.max_worlds)",
    "refused": "Refused by ProvSQL",
    "error": "Fails in ProvSQL",
    "timeout": f"Stopped at the {SERVICE_DEADLINE_SECONDS} s time limit",
    "killed": "Runtime crash: database killed at 3 GB, not stopped by ProvSQL",
    "stopped": "Stopped by provsql.max_memory (1 GB)",
    "disk": "Stopped by temp_file_limit: over 3 GB of temporary files on disk",
    "slow": "Refused before it runs: its plain SQL alone runs past ap-explanation's pre-check (60 s)",
    "rows": ("Refused before it runs: an aggregate receives more rows than ap-explanation's "
             "max_aggregated_rows allows, past which building its provenance can crash the database"),
    "unchecked": "Answered, but the plain SQL answer could not be computed to compare",
}

# What a failure on ProvSQL 1.13 comes from.
BLOCKED_PROVSQL, BLOCKED_APX, BLOCKED_BUG, BLOCKED_SQL, BLOCKED_DATA, BLOCKED_MEMORY = (
    "ProvSQL limit", "ap-explanation", "ProvSQL bug", "The question's SQL", "The demo's data", "Memory / time")
_LIMIT_IN_CTE = ("ProvSQL does not track a LIMIT in a CTE (\"the lowest point\", LIMIT 1 without "
                 "ORDER BY): the answer's provenance is 𝟘 and its value lost")
# Where the runs alone do not tell (probed by hand: the query run by ProvSQL alone, then
# variants with data that answers).
_TEMPLATE_NOTES = {
    **{("T", i): (BLOCKED_PROVSQL, _LIMIT_IN_CTE) for i in ("18", "19", "69", "70", "116", "117", "119")},
    ("C", "301"): (BLOCKED_SQL, "Empty in plain SQL too: its join compares a real latitude to a rounded "
                   "decimal one, which never match"),
    ("C", "303"): (BLOCKED_SQL, "Empty in plain SQL too: its join compares a real latitude to a rounded "
                   "decimal one, which never match"),
    ("K", "245"): (BLOCKED_DATA, "No row on the demo's data; on data with a heat wave, ProvSQL 1.13 "
                   "returns the SQL answer with its provenance (the window functions untracked)"),
    ("N", "256"): (BLOCKED_DATA, "No row on the demo's data; with a threshold the data reaches, ProvSQL "
                   "1.13 returns the SQL answer with its provenance (LAG untracked)"),
    ("H", "339"): (BLOCKED_DATA, "No row on the demo's data; with a threshold the data reaches, the "
                   "adapted query returns the SQL answer"),
}


def _blocked_by(outcome: str, error: str) -> str:
    """What blocks a question on ProvSQL 1.13, from its outcome through ap-explanation."""
    if outcome == "empty":
        return BLOCKED_DATA
    if outcome in ("killed", "stopped", "disk", "timeout", "rows", "slow"):
        return BLOCKED_MEMORY
    if outcome == "apx" and not error.startswith("Aggregates ProvSQL cannot explain"):
        return BLOCKED_APX
    return BLOCKED_PROVSQL  # its limits, the aggregates it cannot explain included


def question_statuses_next(version: str = "1.13") -> list[dict]:
    """:func:`question_statuses` for ProvSQL 1.13, 1.14 or 1.14.0 (*version*), measured through
    ap-explanation master.

    *Status* is measured on the City of Zurich question of the question's template; the
    questions that may exhaust memory were run themselves, on their region's data, with
    provsql.max_memory = '1GB', on ProvSQL 1.13.0; on 1.14 and 1.14.0 those of Attica were run again, the
    others keep their 1.13.0 outcome.
    *Blocked by* says where a failure comes from.
    """
    questions = load_question_set()
    risk: dict[str, list[dict]] = {}
    for r in load_memory_risk():
        risk.setdefault(r["question_id"], []).append(r)
    runs, memory = load_service_runs(version), load_service_memory_runs(version)
    on_113 = {str(r["Id"]): r["Status"] for r in question_statuses_next()} if version != "1.13" else {}
    def region(run: dict) -> str:
        return "its region" if version == "1.13" or run["run"] == version else "its region, ProvSQL 1.13.0"
    zurich = {(q["category"], q["template_index"]): q["question_id"]
              for q in questions if q["question_id"] in runs}
    previous = {str(r["Id"]): r for r in question_statuses()}

    out = []
    for q in questions:
        template = (q["category"], q["template_index"])
        findings = sorted(risk.get(q["question_id"], []), key=lambda r: -r["estimated_mb"])
        worst = findings[0] if findings else None
        z = zurich.get(template, "")
        own = memory.get(q["question_id"])
        if own and own["run"] != version and q["question_id"] in runs:
            own = None  # a City of Zurich question rerun on this build: that run replaces an older one
        run = own or runs.get(z)  # run on its region's data: its own outcome replaces the template's
        outcome = service_outcome(run) if run else ""
        blocked = ""
        if outcome == "unchecked" and own:  # answered within the limits; plain SQL too slow to compare
            status, why = STATUS_OK, "Answered on its region's data; the plain SQL was too slow to compare"
        elif outcome == "ok" or not run:
            if not own and z != q["question_id"] and worst and worst["verdict"] in ("exhausts", "unknown"):
                status, blocked = STATUS_CRASH, BLOCKED_MEMORY
                why = f"Not run on its region's data; estimated to exhaust memory ({worst['size_text']})"
            else:
                status, why = STATUS_OK, worst["size_text"] if worst and worst["verdict"] == "heavy" else ""
        else:
            # stopped by a limit (memory, temporary files, time) or no row in the sample: supported,
            # with caveats; a killed process is a crash
            # the measurement gave up although statement_timeout was set: nothing stopped the
            # statement, which went on in the database
            runaway = (outcome == "timeout" and run["error"].startswith("no result after")
                       and any("statement_timeout" in x for x in run["settings"]))
            status = (STATUS_CRASH if outcome == "killed" or runaway
                      else STATUS_CAVEAT if outcome in ("stopped", "disk", "timeout", "empty", "rows", "slow")
                      else STATUS_UNSUPPORTED)
            why = _SERVICE_WHY.get(outcome, outcome)
            if runaway:
                timeout = [x.split("= ")[1].strip("'") for x in run["settings"] if "statement_timeout" in x][0]
                why = (f"Not stopped: no answer after {run['seconds']} s despite statement_timeout = "
                       f"{timeout}; the statement went on in the database")
            elif outcome == "timeout" and not any("statement_timeout" in x for x in run["settings"]):
                # the measurement gave up; with no statement_timeout, the query went on in the database
                why = (f"Not finished in {SERVICE_DEADLINE_SECONDS} s, and not stopped: it ran without "
                       "statement_timeout, so whether it ends or crashes the database is unknown")
            elif outcome == "killed":
                why += f" ({', '.join(run['lost'])})"
                budget = [x.split("= ")[1].strip("'") for x in run["settings"] if "max_memory" in x]
                why += f", despite provsql.max_memory = {budget[0]}" if budget else ", with no memory budget set"
            elif outcome == "wrong":
                why += f": {run['rows']} rows instead of {run['plain_rows']}"
            elif run["error"]:
                why += f": {run['error'][:110]}"
            blocked = _blocked_by(outcome, run["error"])
            if template in _TEMPLATE_NOTES and not own:
                blocked, why = _TEMPLATE_NOTES[template]
                if outcome == "empty":
                    status = STATUS_UNSUPPORTED if blocked == BLOCKED_SQL else STATUS_CAVEAT
        out.append({
            "Id": int(q["question_id"]), "Category": q["category"], "Question": q["question"],
            "Tables": q["tables"].replace(";", ", "), "Status": status, "Why": why, "Blocked by": blocked,
            "On ProvSQL 1.12": previous[q["question_id"]]["Status"],
            "Estimated memory": format_mb(worst["estimated_mb"]) if worst else "",
            "On ProvSQL 1.13": on_113.get(q["question_id"], ""),
            "Measured on": (f"{q['question_id']} ({region(own)})" if own else z) if run else "",
            "Measured (3 GB cap)": (f"{format_mb(run['peak_backend_mb'])}, {service_outcome(run)}, "
                                    f"{run['seconds']} s") if run else "",
        })
    return out
