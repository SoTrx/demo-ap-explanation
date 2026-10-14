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


# Memory analysis: which questions can make ProvSQL exhaust the database's memory.
# assets/memory_risk.csv comes from scripts/memory_analysis.py, the two measurement files
# from scripts/memtest_group_size.sh and scripts/memtest_questions.py.
MEMORY_RISK_PATH = ASSETS_DIR / "memory_risk.csv"
MEMORY_GROUP_SIZE_PATH = ASSETS_DIR / "memory_group_size.csv"
MEMORY_SWEEP_PATH = ASSETS_DIR / "memory_sweep.jsonl"


def load_memory_risk() -> list[dict]:
    with MEMORY_RISK_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["estimated_mb"] = float(r["estimated_mb"])
    return rows


def load_memory_group_size() -> list[dict]:
    with MEMORY_GROUP_SIZE_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


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


SEED_DATA_DIR = Path(__file__).parent / "dependencies" / "postgres-seed" / "data"
MEMORY_SEMIRING_SERVICE_PATH = ASSETS_DIR / "memory_semiring_service.jsonl"


def seed_row_counts() -> dict[str, int]:
    """Rows of each table of the demo's database, from its seed CSVs (header excluded)."""
    counts = {}
    for path in sorted(SEED_DATA_DIR.glob("*.csv")):
        with path.open() as f:
            counts[path.stem] = sum(1 for _ in f) - 1
    return counts


def load_memory_semiring_service() -> list[dict]:
    """Q295 and 556 run through ap-explanation, one request per semiring."""
    with MEMORY_SEMIRING_SERVICE_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


# Status of a question in the Summary tab, worst first.
STATUS_UNSUPPORTED = "Not supported"
STATUS_MEMORY = "May exhaust memory"
STATUS_OK = "Runs"


def question_statuses() -> list[dict]:
    """Every question of the set with its status and why:

    - Not supported: ap-explanation rejects it, or it fails at run time for another
      reason than memory (a failure measured on one question of a template is assumed
      for the others: their SQL differs only by the region);
    - May exhaust memory: estimated above 3 GB, or not estimable (no elevation data);
    - Runs: the rest, with the estimate when it is above 100 MB.

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
            status = STATUS_MEMORY
            why = worst["size_text"]
        else:
            status = STATUS_OK
            why = worst["size_text"] if worst and worst["verdict"] == "heavy" else ""
        run = sweep.get(q["question_id"])
        out.append({
            "Id": int(q["question_id"]), "Category": q["category"], "Question": q["question"],
            "Tables": q["tables"].replace(";", ", "), "Status": status, "Why": why,
            "Estimated memory": estimate if status != STATUS_UNSUPPORTED else "",
            "Measured (3 GB cap)": (f"{format_mb(run['peak_backend_mb'])}, {run['status']}, {run['seconds']} s"
                                    if run else ""),
        })
    return out


# ProvSQL 1.13, not released yet: the questions run on ProvSQL 1.12 and on its master
# branch. assets/provsql_113_*.jsonl come from scripts/provsql_compare.py, and
# assets/memory_group_size_113.csv from scripts/memtest_group_size.sh on the same build.
PROVSQL_NEXT_COMMIT = "45320ea7"
PROVSQL_NEXT_BUILD = f"master at {PROVSQL_NEXT_COMMIT}, 14 October 2026, 487 commits after 1.12.0"
PROVSQL_NEXT_SWEEP_PATH = ASSETS_DIR / "provsql_113_sweep.jsonl"
PROVSQL_NEXT_MAXMEM_SWEEP_PATH = ASSETS_DIR / "provsql_113_sweep_maxmem1g.jsonl"
PROVSQL_NEXT_CHECKS_PATH = ASSETS_DIR / "provsql_113_checks.jsonl"
MEMORY_GROUP_SIZE_NEXT_PATH = ASSETS_DIR / "memory_group_size_113.csv"

# The three runs of the comparison, as (ProvSQL version, queries adapted to 1.13).
RUN_CURRENT = ("1.12", False)
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


def load_memory_group_size_next() -> list[dict]:
    with MEMORY_GROUP_SIZE_NEXT_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def was_killed(run: dict) -> bool:
    """The kernel killed the database backend: the 3 GB limit was reached."""
    return "server closed the connection" in run["error"]


def run_outcome(run: dict) -> str:
    """What became of a query of the comparison: ``ok`` (it ran and returned the plain SQL
    answer), ``empty`` (the same, but the answer has no row on the demo's data, so no
    provenance was seen), ``wrong`` (not the plain SQL answer), ``limit`` (refused by
    ``provsql.max_worlds``), ``refused``, ``timeout``, ``killed`` or ``error``."""
    if run["status"] == "success":
        if not run["matches_plain"]:
            return "wrong"
        return "ok" if run["plain_rows"] else "empty"
    if run["status"] == "timeout":
        return "timeout"
    if was_killed(run):
        return "killed"
    if run["sqlstate"] == "54000":
        return "limit"
    return "refused" if run["sqlstate"] in ("0A000", "XX000") else "error"


# ap-explanation itself, unchanged (v1.2.0), on ProvSQL 1.13 and 1.12: the City of Zurich
# question of every template (scripts/apx_service_check.py), and on ProvSQL 1.13 the
# questions that may exhaust memory, on the demo's data copied to every cell of their region
# and with provsql.max_memory = '1GB'. The supported City of Zurich questions adapted to
# ProvSQL 1.13 come from scripts/provsql_compare.py.
APX_SERVICE_PATHS = {"1.13": ASSETS_DIR / "apx_service_113.jsonl", "1.12": ASSETS_DIR / "apx_service_112.jsonl",
                     "1.13 patched": ASSETS_DIR / "apx_service_113_patched.jsonl",
                     "1.12 patched": ASSETS_DIR / "apx_service_112_patched.jsonl"}
APX_SERVICE_MEMORY_PATH = ASSETS_DIR / "apx_service_113_memory.jsonl"
APX_SERVICE_MEMORY_PATCHED_PATH = ASSETS_DIR / "apx_service_113_memory_patched.jsonl"
# The ap-explanation branch the patched runs used: its rewriter adapted to ProvSQL 1.13.
APX_PATCH_BRANCH = "feat/provsql-1.13-possible-worlds"
SUPPORTED_ADAPTED_PATH = ASSETS_DIR / "provsql_113_supported_adapted.jsonl"
SERVICE_DEADLINE_SECONDS = 180


def _jsonl_by_question(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["question_id"]: r for r in map(json.loads, filter(str.strip, f))}


def load_service_runs(version: str) -> dict[str, dict]:
    return _jsonl_by_question(APX_SERVICE_PATHS[version])


def load_service_memory_runs(patched: bool = False) -> dict[str, dict]:
    return _jsonl_by_question(APX_SERVICE_MEMORY_PATCHED_PATH if patched else APX_SERVICE_MEMORY_PATH)


def load_supported_adapted() -> dict[str, dict]:
    """The supported City of Zurich questions on ProvSQL 1.13, adapted (scripts/provsql_compare.py)."""
    return _jsonl_by_question(SUPPORTED_ADAPTED_PATH)


def service_outcome(run: dict) -> str:
    """What became of a question sent to ap-explanation: :func:`run_outcome`'s outcomes,
    with ``apx`` (refused by ap-explanation itself) and ``stopped`` (by provsql.max_memory),
    and ``unchecked`` when it answered but the plain SQL answer could not be computed."""
    error = run["error"]
    if run["lost"]:
        return "killed"
    if run["status"] == "timeout":
        return "timeout"
    if run["status"] != "success":
        if error.startswith(("HAVING queries are not supported", "Aggregates ProvSQL cannot explain")):
            return "apx"
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
    "empty": "Runs, but the demo's data has no row for it: its provenance was not seen",
    "wrong": "Not the SQL answer",
    "limit": "Refused by ProvSQL (provsql.max_worlds)",
    "refused": "Refused by ProvSQL",
    "error": "Fails in ProvSQL",
    "timeout": f"Not finished in {SERVICE_DEADLINE_SECONDS} s",
    "killed": "Database killed at 3 GB",
    "stopped": "Stopped by provsql.max_memory (1 GB)",
    "disk": "Stopped by temp_file_limit: over 3 GB of temporary files on disk",
    "unchecked": "Answered, but the plain SQL answer could not be computed to compare",
}

# What a template's failure on ProvSQL 1.13 comes from, where the runs alone do not tell
# (probed by hand: the query run by ProvSQL alone, then variants with data that answers).
BLOCKED_PROVSQL, BLOCKED_APX, BLOCKED_BUG, BLOCKED_SQL, BLOCKED_DATA = (
    "ProvSQL", "ap-explanation", "ProvSQL bug", "The question's SQL", "The demo's data")
_LIMIT_IN_CTE = ("ProvSQL does not track a LIMIT in a CTE (\"the lowest point\", LIMIT 1 without "
                 "ORDER BY): the answer's provenance is 𝟘 and its value lost")
_TEMPLATE_NOTES = {
    **{("T", i): (BLOCKED_PROVSQL, _LIMIT_IN_CTE, "") for i in ("18", "19", "69", "70", "116", "117", "119")},
    ("Q", "361"): (BLOCKED_BUG, "MAX(day) - MIN(day) on dates fails in ProvSQL's agg_token_minus "
                   "(invalid input syntax for type numeric)", "Fix in ProvSQL"),
    ("Q+N", "360"): (BLOCKED_BUG, "Refused by ap-explanation (REGR_SLOPE), and MAX(day) - MIN(day) on dates "
                     "fails in ProvSQL's agg_token_minus", "Fix in ProvSQL"),
    ("C", "301"): (BLOCKED_SQL, "Empty in plain SQL too: its join compares a real latitude to a rounded "
                   "decimal one, which never match", ""),
    ("C", "303"): (BLOCKED_SQL, "Empty in plain SQL too: its join compares a real latitude to a rounded "
                   "decimal one, which never match", ""),
    ("K", "245"): (BLOCKED_APX, "Refused by ap-explanation (HAVING); on data with a heat wave, ProvSQL 1.13 "
                   "returns the SQL answer with its provenance (the window functions untracked)",
                   f"ap-explanation patch ({APX_PATCH_BRANCH})"),
    ("N", "256"): (BLOCKED_DATA, "No row on the demo's data; with a threshold the data reaches, ProvSQL "
                   "1.13 returns the SQL answer with its provenance (LAG untracked)", ""),
    ("H", "339"): (BLOCKED_APX, "No row on the demo's data; with a threshold the data reaches, the query "
                   "as sent returns 8 rows instead of 1, and the SQL answer once adapted",
                   f"ap-explanation patch ({APX_PATCH_BRANCH})"),
}
_FIX_PATCH = f"ap-explanation patch ({APX_PATCH_BRANCH})"


def _blocked_by(template: tuple[str, str], as_is: str, patched: str, adapted_error: str) -> tuple[str, str]:
    """``(what blocks the template on ProvSQL 1.13, what would fix it)``, from its City of
    Zurich question sent to ap-explanation as it is and patched, and ProvSQL's error on the
    query adapted with ap-explanation's refusals lifted (scripts/provsql_compare.py)."""
    if template in _TEMPLATE_NOTES:
        blocked, _, fix = _TEMPLATE_NOTES[template]
        return blocked, fix
    if as_is in ("ok", ""):
        return "", ""
    if as_is == "empty":
        return BLOCKED_DATA, ""
    if patched in ("ok", "empty"):
        return BLOCKED_APX, _FIX_PATCH
    if "Aggregation operator" in adapted_error:
        return BLOCKED_PROVSQL, "None: ProvSQL cannot evaluate this aggregate (ap-explanation's refusal is right)"
    return BLOCKED_PROVSQL, ""


def question_statuses_next() -> list[dict]:
    """:func:`question_statuses` for ProvSQL 1.13, measured through ap-explanation itself.

    - *Status* is what ap-explanation v1.2.0, unchanged, does with the question on ProvSQL
      1.13, measured on the City of Zurich question of its template; the questions that may
      exhaust memory were run themselves, on their region's data, with provsql.max_memory.
    - *Patched* is the same with ap-explanation's rewriter adapted to ProvSQL 1.13
      (branch APX_PATCH_BRANCH: LIMIT plain(k), present(provenance()), HAVING).
    - *Blocked by* and *Fix* say where a failure comes from, and what would remove it.
    """
    questions = load_question_set()
    risk: dict[str, list[dict]] = {}
    for r in load_memory_risk():
        risk.setdefault(r["question_id"], []).append(r)
    as_is, memory = load_service_runs("1.13"), load_service_memory_runs()
    patched, memory_patched = load_service_runs("1.13 patched"), load_service_memory_runs(patched=True)
    adapted = load_provsql_sweep().get(RUN_NEXT_ADAPTED, {}) | load_supported_adapted()
    zurich = {(q["category"], q["template_index"]): q["question_id"]
              for q in questions if q["question_id"] in as_is}
    previous = {str(r["Id"]): r for r in question_statuses()}

    def status_of(outcome: str, worst: dict | None, ran_before: bool, own_run: bool = False) -> tuple[str, str]:
        if outcome == "empty" and ran_before:  # as on ProvSQL 1.12: no row on the demo's data
            outcome = "ok"
        if own_run and outcome == "timeout":  # on its region's data, still growing when stopped
            return STATUS_MEMORY, f"{_SERVICE_WHY['timeout']} on its region's data"
        if outcome == "ok":
            if worst and worst["verdict"] in ("exhausts", "unknown"):
                return STATUS_MEMORY, f"Not run on its region's data; {worst['size_text']}"
            return STATUS_OK, worst["size_text"] if worst and worst["verdict"] == "heavy" else ""
        return (STATUS_MEMORY if outcome in ("killed", "stopped", "disk") else STATUS_UNSUPPORTED), _SERVICE_WHY.get(outcome, outcome)

    out = []
    for q in questions:
        template = (q["category"], q["template_index"])
        findings = sorted(risk.get(q["question_id"], []), key=lambda r: -r["estimated_mb"])
        worst = findings[0] if findings else None
        z = zurich.get(template, "")
        z_run, z_adapted = as_is.get(z), adapted.get(z)
        outcome = service_outcome(z_run) if z_run else ""
        outcome_patched = service_outcome(patched[z]) if z in patched else outcome
        own, own_patched = memory.get(q["question_id"]), memory_patched.get(q["question_id"])
        if own:  # run on its region's data: its own outcome replaces the template's
            outcome = service_outcome(own)
            if own_patched:
                outcome_patched = service_outcome(own_patched)
            elif outcome in ("killed", "stopped", "disk", "timeout", "limit"):
                outcome_patched = outcome  # the patch does not change the query's aggregates
        ran_before = previous[q["question_id"]]["Status"] == STATUS_OK
        status, why = status_of(outcome, None if own else worst, ran_before, own_run=bool(own))
        if own and status == STATUS_MEMORY:
            lost = ", ".join(own["lost"])
            why = f"{why}{f' ({lost})' if lost else ''}, despite provsql.max_memory = 1GB" if outcome == "killed" else why
        elif status != STATUS_OK and outcome not in ("ok", "") and (own or z_run):
            source = own or z_run
            detail = (source["error"] if outcome not in ("wrong",) else
                      f"{source['rows']} rows instead of {source['plain_rows']}")
            why = f"{why}: {detail[:110]}" if detail else why
        if template in _TEMPLATE_NOTES and status != STATUS_OK:
            why = _TEMPLATE_NOTES[template][1]
        status_patched, _ = status_of(outcome_patched, None if own_patched or outcome_patched != "ok" else worst,
                                      ran_before, own_run=bool(own))
        blocked, fix = (("", "") if status == STATUS_OK else
                        _blocked_by(template, outcome, outcome_patched, (z_adapted or {}).get("error", "")))
        if own and outcome in ("killed", "stopped", "disk", "timeout"):
            blocked, fix = BLOCKED_PROVSQL, ""
        measured = own or z_run
        out.append({
            "Id": int(q["question_id"]), "Category": q["category"], "Question": q["question"],
            "Tables": q["tables"].replace(";", ", "), "Status": status, "Why": why,
            "Patched": status_patched, "Blocked by": blocked, "Fix": fix,
            "On ProvSQL 1.12": previous[q["question_id"]]["Status"],
            "Estimated memory": format_mb(worst["estimated_mb"]) if worst else "",
            "Measured on": (f"{q['question_id']} (its region)" if own else z) if measured else "",
            "Measured (3 GB cap)": (f"{format_mb(measured['peak_backend_mb'])}, {service_outcome(measured)}, "
                                    f"{measured['seconds']} s") if measured else "",
        })
    return out
