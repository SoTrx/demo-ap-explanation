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
# (fixtures/meteo_queries.csv there; the SQL is kept for unsupported questions only).
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
        "exhaust memory (see Memory analysis)."
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
