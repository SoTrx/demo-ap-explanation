"""Find the meteo questions whose provenance can exhaust the database's memory, and why.

Two query shapes make ProvSQL's formula evaluation outgrow memory (measured with
scripts/memtest_group_size.sh and scripts/memtest_questions.py on a 3 GB-capped database):

- **Comparison on an aggregate computed below the top level** (a CTE or subquery, e.g.
  ``WITH m AS (SELECT month, SUM(tp) AS total ... GROUP BY month) SELECT ... WHERE total >
  0.001``, or a HAVING). ProvSQL expands the comparison over the combinations of the
  group's readings: memory grows about 3x per reading in the group (0.9 GB at 20, over
  3 GB at 22).
- **Aggregate over many rows**: the formula lists every row the aggregate reads, about
  3 KB each (400 MB for the 134,280 elevation points of a cell), and memory follows the
  largest group. Rows multiply when readings are joined to the elevation table: each
  reading is paired with every point of its cell the query keeps, about 2 KB per pair
  (1.85 GB for 31 readings x 33,096 points).

This script finds both in every question of the set, estimates the memory, and writes
assets/memory_risk.csv, one row per question and shape found.

Usage (sqlglot is in the dev group):
    uv run python scripts/memory_analysis.py ../../ap-explanation/fixtures/meteo_queries.csv
"""
import csv
import re
import sys
from pathlib import Path

from sqlglot import exp, parse_one

ROOT = Path(__file__).parent.parent
OUT = ROOT / "assets" / "memory_risk.csv"
# The only elevation data at hand: the 134,280 points of the City of Zurich cell.
ELEVATION_CSV = ROOT / "dependencies" / "postgres-seed" / "data" / "meteo_elevation.csv"

AGGREGATES = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)
COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
READING_TABLES = {"meteo_tp", "meteo_tmin", "meteo_tmax", "meteo_tmean",
                  "meteo_windspeedmax", "meteo_sdmax", "meteo_ssrd"}
# Readings per cell in one group, by the finest time unit of the GROUP BY (at most).
READINGS_PER_CELL = {"day": 1, "week": 7, "month": 31, "year": 366}

# Measured: peak backend memory of a comparison on SUM over n readings (MB).
_COMPARISON_MB = {16: 50, 18: 95, 20: 900}
_COMPARISON_GROWTH = 3.0  # per extra reading, from 18 -> 20
MB_PER_PAIR = 0.002       # 1.85 GB / 1,025,976 pairs (question 152)
MB_PER_ROW = 0.003        # 400 MB / 134,280 elevation points (question 556)
SAFE_MB, HEAVY_MB = 100, 3 * 1024

_PAIR_RE = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\)")
_ELEV_FILTER_RE = re.compile(r"elevation\s*(>=|<=|>|<)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _load_elevations() -> list[float]:
    with ELEVATION_CSV.open(newline="") as f:
        return [float(r["elevation"]) for r in csv.DictReader(f)]


ELEVATIONS = _load_elevations()


def comparison_mb(readings: int) -> float:
    if readings <= 16:
        return _COMPARISON_MB[16] if readings > 8 else 40
    if readings <= 18:
        return _COMPARISON_MB[18]
    # Past ~30 more readings the estimate is beyond any machine: call it unbounded.
    if readings - 20 > 30:
        return float("inf")
    return _COMPARISON_MB[20] * _COMPARISON_GROWTH ** (readings - 20)


def verdict(mb: float) -> str:
    return "fine" if mb < SAFE_MB else "heavy" if mb < HEAVY_MB else "exhausts"


def _cells(select_sql: str, query_sql: str) -> int:
    """Cells of the region filter: the (lat, lon) pairs of its IN list."""
    def pairs(sql: str) -> set[str]:
        return {re.sub(r"\s", "", p) for p in _PAIR_RE.findall(sql)}
    return len(pairs(select_sql)) or len(pairs(query_sql)) or 1


def _own_tables(select: exp.Select) -> list[str]:
    """Tables and CTE names read by this SELECT itself, not by its nested SELECTs."""
    return [t.name.lower() for t in select.find_all(exp.Table) if t.find_ancestor(exp.Select) is select]


def _aggregate_columns(select: exp.Select) -> dict[str, exp.Expression]:
    out = {}
    for proj in select.expressions:
        agg = proj.find(*AGGREGATES)
        if agg and not proj.find(exp.Window):
            out[proj.alias_or_name.lower()] = agg
    return out


def _named_sources(tree: exp.Expression) -> dict[str, exp.Select]:
    sources = {}
    for cte in tree.find_all(exp.CTE):
        if isinstance(cte.this, exp.Select):
            sources[cte.alias_or_name.lower()] = cte.this
    for sub in tree.find_all(exp.Subquery):
        if sub.alias and isinstance(sub.this, exp.Select):
            sources[sub.alias.lower()] = sub.this
    return sources


def _time_unit(select: exp.Select) -> str | None:
    group = select.args.get("group")
    units = set()
    for e in (group.expressions if group else []):
        sql = e.sql().upper()
        if "DATE(" in sql or re.fullmatch(r"(\w+\.)?TIME", sql):
            units.add("day")
        for unit in ("WEEK", "MONTH", "YEAR"):
            if f"EXTRACT({unit}" in sql:
                units.add(unit.lower())
    return min(units, key=READINGS_PER_CELL.get) if units else None


def _days_in_window(sql: str) -> tuple[int, str]:
    """Days of readings per cell the query's time filter keeps, and how it was read."""
    s = sql.upper()
    years = 1
    if m := re.search(r"EXTRACT\(YEAR FROM [^)]*\)\s*BETWEEN\s*(\d{4})\s*AND\s*(\d{4})", s):
        years = int(m[2]) - int(m[1]) + 1
    elif not re.search(r"EXTRACT\(YEAR FROM [^)]*\)\s*=\s*\d{4}|\d{4}-\d{2}-\d{2}", s):
        return 15 * 366, "every day (15 years in the demo data, more in the full dataset)"
    if re.search(r"DATE\([^)]*\)\s*=|\bTIME\)?\s*=\s*'|CAST\([^)]* AS DATE\)\s*=|EXTRACT\(DAY FROM [^)]*\)\s*=", s):
        return years, f"{years} day(s)"
    if re.search(r"EXTRACT\(WEEK FROM [^)]*\)\s*(=|BETWEEN|IN\b)", s):
        return 7 * years, f"{years} week(s)"
    if m := re.search(r"EXTRACT\(MONTH FROM [^)]*\)\s*BETWEEN\s*(\d+)\s*AND\s*(\d+)", s):
        months = int(m[2]) - int(m[1]) + 1
        return 31 * months * years, f"{months} month(s) × {years} year(s)"
    if re.search(r"EXTRACT\(MONTH FROM [^)]*\)\s*(=|IN\b)", s):
        return 31 * years, f"1 month × {years} year(s)"
    return 366 * years, f"{years} year(s)"


def _points_kept(sql: str) -> tuple[int, str]:
    """Elevation points of the Zurich cell passing the query's elevation filters."""
    filters = _ELEV_FILTER_RE.findall(sql)
    ops = {">=": float.__ge__, "<=": float.__le__, ">": float.__gt__, "<": float.__lt__}
    kept = sum(all(ops[op](e, float(v)) for op, v in filters) for e in ELEVATIONS)
    how = " and ".join(f"elevation {op} {v}" for op, v in filters) or "no elevation filter"
    return kept, how


def _groups_by_cell(select: exp.Select) -> bool:
    group = select.args.get("group")
    return bool(group) and any(
        c.name.lower() in ("latitude", "longitude") for e in group.expressions for c in e.find_all(exp.Column))


def comparisons(tree: exp.Expression, sql: str) -> list[dict]:
    """Comparisons on an aggregate computed below them: a column of an aggregating CTE or
    subquery, a scalar subquery (``x = (SELECT MIN(...) ...)``), or a HAVING."""
    found = []
    sources = _named_sources(tree)
    for cmp in tree.find_all(*COMPARISONS):
        source = agg = None
        if cmp.find_ancestor(exp.Having):
            source, agg = cmp.find_ancestor(exp.Select), cmp.find(*AGGREGATES)
        elif cmp.find_ancestor(exp.Where, exp.Join):
            for side in (cmp.left, cmp.right):
                sub = side if isinstance(side, exp.Subquery) else side.find(exp.Subquery)
                if sub is not None and isinstance(sub.this, exp.Select) and _aggregate_columns(sub.this):
                    source, agg = sub.this, next(iter(_aggregate_columns(sub.this).values()))
            for col in cmp.find_all(exp.Column):
                for name, select in sources.items():
                    a = _aggregate_columns(select).get(col.name.lower())
                    if a is not None and col.table.lower() in (name, "") and select is not cmp.find_ancestor(exp.Select):
                        source, agg = select, a
        if agg is None:
            continue
        cells, unit = _cells(source.sql(), sql), _time_unit(source)
        if "meteo_elevation" in set(_own_tables(source)) and "elevation" in agg.sql().lower():
            points, how = _points_kept(source.sql())
            readings = points * cells
            size = f"{readings:,} elevation points per compared group = {cells} cell(s) × {points:,} ({how})"
        elif unit:
            readings = cells * READINGS_PER_CELL[unit]
            size = (f"{readings:,} readings per compared group = {cells} cell(s) × "
                    f"{READINGS_PER_CELL[unit]} per {unit}")
        else:
            days, how = _days_in_window(source.sql())
            readings = cells * days
            size = f"{readings:,} readings per compared group = {cells} cell(s) × {how}"
        found.append({"shape": "comparison",
                      "detail": f"{cmp.sql(dialect='postgres')[:120]}  —  on {agg.sql(dialect='postgres')}",
                      "size": readings, "size_text": size,
                      "estimated_mb": comparison_mb(readings)})
    return found


def large_aggregates(tree: exp.Expression, sql: str) -> list[dict]:
    """Aggregates whose largest group holds many rows: every row is a term of the formula.
    Rows multiply when readings are joined to the elevation points of their cell."""
    sources = _named_sources(tree)

    def reads(select: exp.Select, tables: set[str], depth: int = 0) -> bool:
        """Whether the SELECT reads one of these tables row by row (not through an
        aggregate, a DISTINCT or a LIMIT, which narrow them down)."""
        for name in _own_tables(select):
            if name in tables:
                return True
            src = sources.get(name)
            if (src is not None and depth < 5 and not src.args.get("limit")
                    and not src.args.get("distinct") and not _aggregate_columns(src)
                    and reads(src, tables, depth + 1)):
                return True
        return False

    best = None
    for select in tree.find_all(exp.Select):
        aggs = _aggregate_columns(select)
        elevation, readings = reads(select, {"meteo_elevation"}), reads(select, READING_TABLES)
        if not aggs or not (elevation or readings):
            continue
        cells = 1 if _groups_by_cell(select) else _cells(select.sql(), sql)
        unit = _time_unit(select)
        if unit and readings:
            days, window = READINGS_PER_CELL[unit], f"1 {unit}"
        else:
            days, window = _days_in_window(sql)
        points, how = _points_kept(sql)
        agg = next(iter(aggs.values())).sql(dialect="postgres")
        if elevation and readings:
            rows, mb = days * cells * points, days * cells * points * MB_PER_PAIR
            text = f"{rows:,} reading × point pairs in the largest group = {window} × {cells} cell(s) × {points:,} points ({how})"
        elif elevation:
            rows, mb = cells * points, cells * points * MB_PER_ROW
            text = f"{rows:,} elevation points in the largest group = {cells} cell(s) × {points:,} ({how})"
        else:
            rows, mb = days * cells, days * cells * MB_PER_ROW
            text = f"{rows:,} readings in the largest group = {window} × {cells} cell(s)"
        if best is None or mb > best["estimated_mb"]:
            best = {"shape": "large aggregate", "detail": f"{agg} over {'readings × elevation points' if elevation and readings else 'elevation points' if elevation else 'readings'}",
                    "size": rows, "size_text": text, "estimated_mb": mb,
                    # Zurich's cell is the only elevation data at hand: when none of its
                    # points pass the filter (above 2500 m), it says nothing about another
                    # region's.
                    "unknown": elevation and points == 0 and cells > 1,
                    "elevation": elevation}
    # Only the ones worth listing: heavy or worse, or involving elevation points.
    if best and (best["estimated_mb"] >= SAFE_MB or best.pop("elevation")):
        best.pop("elevation", None)
        return [best]
    return []


def main(path: str) -> None:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    results = []
    for row in rows:
        tree = parse_one(row["sql"], read="postgres")
        # The largest compared group, and the largest aggregate if it matters.
        hits = sorted(comparisons(tree, row["sql"]), key=lambda h: h["size"])[-1:]
        hits += large_aggregates(tree, row["sql"])
        for hit in hits:
            unknown = hit.pop("unknown", False)
            mb = hit.pop("estimated_mb")
            results.append({
                "question_id": row["question_id"], "category": row["category"],
                "question": row["question"], "tables": row["tables"],
                "supported": row["supported"], **hit,
                "estimated_mb": round(mb, 1) if mb != float("inf") else "inf",
                "verdict": "unknown" if unknown else verdict(mb)})
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print(f"{len(results)} findings in {len({r['question_id'] for r in results})} questions -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
