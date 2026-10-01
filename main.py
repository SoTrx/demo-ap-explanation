import asyncio
import json
from collections import Counter

import httpx
import pandas as pd
import psycopg
import streamlit as st
from kiota_abstractions.api_error import APIError

from api import (
    AP_EXPLANATION_SERVICE_URL,
    DbMonitor,
    explain,
    run_plain,
    service_healthy,
)
from utils import (
    STATUS_MEMORY,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    UNSUPPORTED_REASONS,
    ap_tables,
    cited_readings,
    database_name,
    extract_sql,
    format_mb,
    format_reading,
    load_ap,
    load_memory_group_size,
    load_memory_risk,
    load_memory_semiring_service,
    load_memory_sweep,
    load_presets,
    load_question_set,
    load_recorded,
    question_statuses,
    reading_labels,
    seed_row_counts,
    shorten_formula,
    stamp_start_time,
    unsupported_reason,
)

TITLE = "Provenance Explorer: Zurich weather"

st.set_page_config(page_title=TITLE, layout="wide")
st.title(TITLE)
st.markdown(
    "Weather questions asked in SQL over ERA5 daily readings, explained by "
    "[`ap-explanation`](https://github.com/datagems-eosc/ap-explanation): the plain SQL "
    "answer, then what the **provenance** adds, the exact readings behind every result row "
    "computed by ProvSQL, and an LLM explanation written from them."
)

_PRESET_PLACEHOLDER = "— Select a question —"
_PRESETS = load_presets()
_PRESET_BY_LABEL = {f"{p['id']} · {p['question']}": p for p in _PRESETS}
_PRESET_LABELS = [_PRESET_PLACEHOLDER] + list(_PRESET_BY_LABEL)

_ALL_SEMIRINGS = "All semirings"
_SEMIRINGS = [_ALL_SEMIRINGS, "formula", "why", "how", "which", "boolexpr"]

_STATUS_ICON = {"success": "✅", "failure": "❌",
                "revoked": "⏹️", "timeout": "⌛"}


@st.cache_data(ttl=15, show_spinner=False)
def _service_up() -> bool:
    return service_healthy()


def _short(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= 200 else text[:197] + "…"


def _answer_summary(answer: dict) -> str:
    return ", ".join(f"{k} = {_short(v)}" for k, v in answer.items())


def _render_plain(plain: dict | None, plain_error: str | None) -> None:
    st.subheader("1. Plain SQL answer")
    if plain_error:
        st.warning(f"Couldn't run the query plainly: {plain_error}")
        return
    if not plain:
        st.info("No plain answer: `PLAIN_DSN` is not set on this instance.")
        return
    columns, rows = plain.get("columns") or [], plain.get("rows") or []
    st.caption(
        f"{len(rows)} row(s) in {plain.get('seconds', '?')} s. This is all a user "
        "gets from SQL alone.")
    st.dataframe([dict(zip(columns, row)) for row in rows], width="stretch")


def _render_derivation(derivation: dict) -> None:
    provenance = derivation.get("provenance") or {}
    # Aggregate queries only have ``formula``; it is also the one citing the most.
    semiring = "formula" if "formula" in provenance else next(
        iter(provenance), None)
    if semiring is None:
        st.info("No provenance for this row.")
        return
    readings = cited_readings(derivation, semiring)
    labels = reading_labels(readings)

    st.markdown(f"**Provenance** (`{semiring}` semiring)")
    st.code(shorten_formula(provenance[semiring].get("expression", ""), labels),
            language=None, wrap_lines=True)
    others = [s for s in provenance if s != semiring]
    if others:
        st.table([
            {"Semiring": s,
             "Expression": _short(shorten_formula(provenance[s].get("expression", ""), labels))}
            for s in others
        ])

    st.markdown(f"**Cited readings** — {len(readings)}")
    st.dataframe(
        [format_reading(labels[r["reference"]], r["data"]) for r in readings],
        width="stretch", hide_index=True,
        height="auto" if len(readings) <= 10 else 388,
    )
    if derivation.get("probability") is not None:
        st.caption(f"Probability: {derivation['probability']}")


def _render_cost(preset: dict, response: dict, source: str) -> None:
    """Time and database memory the run took."""
    total = response.get("seconds")
    plain = response.get("plain") or {}
    db = response.get("db") or {}
    measured = db.get("peak_mb") is not None

    c_total, c_db_time, c_db_mem, c_plain = st.columns(4)
    c_total.metric(
        "Total time", f"{total} s" if total is not None else "—",
        help="From submitting the AP to the task settling: annotating the tables, "
             "computing the provenance and asking the LLM.")
    c_db_time.metric(
        "Database time", f"{db['busy_seconds']} s" if measured else "—",
        help="How long one of the run's PostgreSQL backends was running a statement: "
             "annotation, provenance query, fetching the cited rows. Sampled every 0.1 s.")
    c_db_mem.metric(
        "Peak database memory", f"{db['peak_mb']} MB" if measured else "—",
        help="Private memory (RssAnon) of the run's PostgreSQL backends, summed, at its "
             "highest sample (every 0.1 s). Shared buffers are not counted.")
    c_plain.metric(
        "Plain SQL time", f"{plain['seconds']} s" if plain.get(
            "seconds") is not None else "—",
        help="The same query run without provenance.")

    if source == "recorded":
        st.caption(preset.get("recorded_cost_note")
                   or "Database time and memory are only measured on live runs.")
    elif measured and not db["busy_seconds"]:
        st.caption(
            "The database work was shorter than the 0.1 s sampling interval.")
    elif measured and total is not None:
        st.caption(
            f"About {max(total - db['busy_seconds'], 0):.1f} s of the run were spent outside "
            "the database, mostly in the LLM call.")
    elif db.get("error"):
        st.caption(f"Database time and memory unavailable: {db['error']}")


def _render_response(preset: dict, response: dict, source: str) -> None:
    status = response.get("status", "?")
    seconds = response.get("seconds")
    origin = "Recorded run" if source == "recorded" else "Live run"
    took = f" in {seconds} s" if seconds is not None else ""
    _banner = st.success if status == "success" else st.error
    _banner(f"{_STATUS_ICON.get(status, '')} **{origin}:** `{status}`{took}"
            + (f" · `{response['request']}`" if response.get("request") else ""))
    _render_cost(preset, response, source)

    _render_plain(response.get("plain"), response.get("plain_error"))

    st.subheader("2. What provenance adds")
    result = response.get("result") or {}
    derivations = result.get("derivations") or []
    if source == "recorded" and preset.get("takeaway"):
        (st.info if derivations else st.warning)(preset["takeaway"])
    if status != "success":
        st.error("Provenance not computed.")
        if response.get("error"):
            st.code(_short(response["error"]) if not isinstance(response["error"], str)
                    else response["error"], language=None, wrap_lines=True)
    elif not derivations:
        st.info("The query returned no rows, so there is nothing to explain.")
    else:
        st.caption(
            "Formula notation: `table#n` is one cited reading (listed below the formula), "
            "`⊗` a join of readings, `+` independent derivations, `*n` a reading's value in "
            "an aggregate (`COUNT` gives `*1`), `max(…)` a MAX aggregate. Aggregate values "
            "come back as ProvSQL text, e.g. `'13 (*)'`, and an unnamed aggregate column "
            "as `col_0`.")
        for i, derivation in enumerate(derivations, 1):
            with st.expander(f"Row {i}: {_answer_summary(derivation.get('answer') or {})}",
                             expanded=len(derivations) <= 3 or i == 1):
                _render_derivation(derivation)

    st.subheader("3. LLM explanation")
    explanation = result.get("explanation")
    if explanation and explanation != "No explanation":
        st.caption(
            "Written by ap-explanation's LLM from the query, its answer and the provenance.")
        with st.container(border=True):
            st.markdown(explanation)
    else:
        st.info("No explanation: the run failed, or the service has no LLM "
                "configured (`LLM_API_BASE`).")

    with st.expander("Raw ap-explanation response (JSON)"):
        st.json(response, expanded=False)


def _render_question(preset: dict) -> None:
    """The SQL of the picked question, its run buttons, and the last run's result."""
    ap_id = preset["id"]
    ap_data = load_ap(ap_id)
    st.caption(f"**{preset['category']}** — {preset['description']}")

    st.divider()

    st.subheader("SQL")
    st.code(extract_sql(ap_data)
            or "-- no Provenance_SQL_Operator query", language="sql")
    st.caption(
        "Tables: " +
        ", ".join(f"`{t}`" for t in ap_tables(ap_data)) + ". ap-explanation "
        "annotates them with provenance, then runs the query under ProvSQL.")

    st.divider()

    semiring = st.selectbox(
        "Semirings", _SEMIRINGS, key="semiring",
        help="Which provenance semirings to compute on a live run. Aggregate queries "
             "(COUNT, MAX…) are explained with `formula` only.")

    service_up = _service_up()
    if not preset.get("live", True):
        st.warning(
            "Live runs are disabled for this question: ProvSQL evaluates its comparison on an "
            "aggregate by exhausting the database's memory, and ignores cancellation. "
            "Show the recorded run instead.")
    elif not service_up:
        st.warning(
            f"ap-explanation is not reachable at `{AP_EXPLANATION_SERVICE_URL}`, live runs are "
            "disabled. Show the recorded run instead.")

    col_run, col_recorded = st.columns(2)
    run_clicked = col_run.button(
        "▶️ Run on ap-explanation", type="primary", width="stretch",
        disabled=not (service_up and preset.get("live", True)))
    recorded = load_recorded(ap_id)
    recorded_clicked = col_recorded.button(
        "📼 Show recorded run", width="stretch", disabled=recorded is None,
        help="Replay the response ap-explanation gave on this question when the demo was made.")

    if run_clicked:
        plain, plain_error = None, None
        try:
            with st.spinner("Running the query plainly…"):
                plain = run_plain(ap_data)
        except psycopg.Error as exc:
            plain_error = str(exc).strip()
        try:
            with (
                st.spinner("Annotating the tables, computing the provenance and asking the "
                           "LLM to explain it… (usually 15–40 s)"),
                DbMonitor(database_name(ap_data)) as monitor,
            ):
                response = asyncio.run(explain(
                    stamp_start_time(ap_data),
                    None if semiring == _ALL_SEMIRINGS else semiring))
            response |= {"plain": plain,
                         "plain_error": plain_error, "db": monitor.report()}
            st.session_state["last_run"] = {
                "ap_id": ap_id, "source": "live", "response": response}
        except (httpx.HTTPError, ConnectionError, OSError):
            st.error(f"Couldn't reach ap-explanation at `{AP_EXPLANATION_SERVICE_URL}`. "
                     "Check `AP_EXPLANATION_SERVICE_URL`.")
        except APIError as exc:
            st.error(
                f"Explanation request failed with status {exc.response_status_code}")
        except TimeoutError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors
            st.error(f"Explanation error: {exc}")
    elif recorded_clicked:
        st.session_state["last_run"] = {
            "ap_id": ap_id, "source": "recorded", "response": recorded}

    last_run = st.session_state.get("last_run")
    if last_run and last_run["ap_id"] == ap_id:
        _render_response(preset, last_run["response"], last_run["source"])


def _render_summary() -> None:
    """How many questions of the meteo question set ap-explanation supports, and why
    the others are not."""
    questions = load_question_set()
    unsupported = [q for q in questions if q["supported"] != "true"]
    total, n_ko = len(questions), len(unsupported)
    n_ok = total - n_ko

    st.markdown(
        f"The meteo question set holds **{total}** natural-language questions with their "
        "SQL. ap-explanation's `scripts/classify_meteo_queries.py` runs each one through the "
        "provenance pipeline (its SQL rewriter, then ProvSQL) on empty copies of the meteo "
        "tables. A question is **unsupported** when that fails, or when ProvSQL warns that "
        "the provenance it returns would be incomplete.")

    c_total, c_ok, c_ko = st.columns(3)
    c_total.metric("Questions", total)
    c_ok.metric("Supported", f"{n_ok} ({n_ok / total:.0%})")
    c_ko.metric("Unsupported", f"{n_ko} ({n_ko / total:.0%})")

    by_reason: dict[str, list[dict]] = {}
    for q in unsupported:
        by_reason.setdefault(unsupported_reason(q["reason"])[0], []).append(q)
    why_by_name = {name: why for _, name, why in UNSUPPORTED_REASONS}
    counts = Counter({name: len(qs) for name, qs in by_reason.items()})

    st.subheader("Why questions are unsupported")
    st.table([
        {"Reason": name, "Questions": count, "Share of unsupported": f"{count / n_ko:.0%}",
         "Why": why_by_name.get(name, "")}
        for name, count in counts.most_common()
    ])

    st.subheader("Unsupported questions, by reason")
    for name, count in counts.most_common():
        qs = by_reason[name]
        with st.expander(f"{name} — {count} questions"):
            st.markdown(why_by_name.get(name, ""))
            st.dataframe(
                [{"Id": q["question_id"], "Category": q["category"], "Question": q["question"],
                  "Tables": q["tables"].replace(";", ", ")} for q in qs],
                width="stretch", hide_index=True)
            st.markdown(
                f"**Example** — question {qs[0]['question_id']}: {qs[0]['question']}")
            st.code(qs[0]["sql"], language="sql")
            st.caption(f"Recorded error: {qs[0]['reason']}")

    st.caption(
        "Supported means the query is rewritten and evaluated; it does not promise the "
        "evaluation stays tractable on real data. Q295 is supported, yet its comparison on "
        "an aggregate exhausts the database's memory (see *Memory analysis*).")


_VERDICT_LABEL = {
    "exhausts": "🔴 Exhausts memory (> 3 GB)",
    "heavy": "🟠 Heavy (0.1–3 GB)",
    "fine": "🟢 Fine (< 100 MB)",
    "unknown": "⚪ Unknown (no elevation data for the region)",
}
_SHAPE_LABEL = {
    "comparison": "Comparison on a nested aggregate",
    "large aggregate": "Aggregate over many rows",
}
_ELEVATION_JOIN_EXAMPLE = """SELECT AVG(tp.tp) * 1000 AS average_total_precipitation_mm
FROM meteo_tp AS tp
INNER JOIN meteo_elevation AS e
  ON ROUND(CAST(tp.latitude AS DECIMAL), 1) = ROUND(CAST(e.latitude AS DECIMAL), 1)
  AND ROUND(CAST(tp.longitude AS DECIMAL), 1) = ROUND(CAST(e.longitude AS DECIMAL), 1)
WHERE EXTRACT(MONTH FROM tp.time) = 1 AND EXTRACT(YEAR FROM tp.time) = 2018
  AND (ROUND(CAST(tp.latitude AS DECIMAL), 1), ROUND(CAST(tp.longitude AS DECIMAL), 1)) IN ((47.4, 8.5))
  AND e.elevation >= 500"""


def _measured(run: dict | None) -> str:
    if not run:
        return "—"
    if run["status"] == "success":
        return f"{format_mb(run['peak_backend_mb'])} in {run['seconds']} s"
    if run["status"] == "timeout":
        return f"stopped after {run['seconds']} s, at {format_mb(run['peak_backend_mb'])}"
    if "server closed the connection" in run["error"]:
        return f"killed at {format_mb(run['peak_backend_mb'])} after {run['seconds']} s"
    return f"failed: {run['error'][:60]}"


def _risk_table(rows: list[dict], sweep: dict[str, dict]) -> None:
    st.dataframe(
        [{"Id": r["question_id"], "Question": r["question"],
          "Verdict": _VERDICT_LABEL[r["verdict"]],
          "Estimated memory": format_mb(r["estimated_mb"]),
          "Measured (3 GB cap)": _measured(sweep.get(r["question_id"])),
          "Shape": _SHAPE_LABEL[r["shape"]], "Why": r["size_text"]}
         for r in sorted(rows, key=lambda r: (-r["estimated_mb"], -float(r["size"])))],
        width="stretch", hide_index=True)


def _peak(c: dict) -> str:
    """Peak memory of a benchmark run: runs under 0.3 s end before the first sample."""
    if c["peak_backend_mb"] in ("", "0"):
        return "< 0.3 s"
    return format_mb(float(c["peak_backend_mb"]))


def _render_memory_setup() -> None:
    """The 3 GB limit, and the databases the measurements ran on."""
    st.subheader("How it was measured")
    st.markdown(
        "**The memory limit is 3 GB.** Every measurement on this page ran on a throwaway "
        "PostgreSQL 17 + ProvSQL 1.12 server (`ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0`), "
        "started with `docker run --memory=3g --memory-swap=3g`: **3 GB of RAM and no swap for "
        "the whole database server**. A query that needs more is killed by the kernel's OOM "
        "killer; that is what *exhausts memory* means here. The verdicts follow from it: "
        "**fine** under 100 MB, **heavy** from 100 MB to 3 GB (the run completes), **exhausts** "
        "above 3 GB. The peak memory is the database backend's (`VmHWM` in `/proc`), sampled "
        "every 0.2–0.3 s. A machine with more memory moves the line, not the shapes: memory "
        "grows exponentially with shape 1, so a few more readings use up any machine.")
    counts = seed_row_counts()
    st.markdown("The server held two databases:")
    st.table([
        {"Database": "memtest", "Table": "t_wet_<n>, t_dry_<n>, one per test", "Rows": "n, from 4 to 31",
         "Built from": "`generate_series(1, n)`: n readings in a single group, values of 1 to 9 mm "
                       "(*wet*), or 4 in 5 at 0 mm (*dry*), annotated with `add_provenance`. "
                       "Used for shape 1 (`scripts/memtest_group_size.sh`)."},
        *[{"Database": "meteo", "Table": table, "Rows": f"{counts[table]:,}",
           "Built from": ("Daily readings of the City of Zurich ERA5 cell (47.4, 8.5), "
                          "2005-01-01 to 2019-12-31, from the Open-Meteo archive (model "
                          "`era5_seamless`), in ERA5 units.")}
          for table in ("meteo_tmin", "meteo_tmax", "meteo_tp", "meteo_windspeedmax") if table in counts],
        {"Database": "meteo", "Table": "meteo_elevation", "Rows": f"{counts.get('meteo_elevation', 0):,}",
         "Built from": "The elevation points of the same cell, copied from the dev server's "
                       "`public.meteo_elevation_zurich`."},
    ])
    st.caption(
        "Database `meteo` is the demo's own, loaded from `dependencies/postgres-seed/` "
        "(`01_meteo.sql`). The questions ran through ap-explanation built from its repository "
        "(the sql_rewriter changes after v1.1.1), with `probability=false` and no LLM, so time "
        "and memory are the provenance's alone (`scripts/memtest_questions.py`).")


_SEMIRING_ORDER = ["formula", "why", "how",
                   "which", "boolexpr", "probability_evaluate"]


def _render_semirings(curve: list[dict]) -> None:
    """Which provenance semirings each shape concerns, measured."""
    st.subheader("Which semirings are concerned")
    st.markdown(
        "ap-explanation computes five semirings: `formula`, `why`, `how`, `which` and "
        "`boolexpr`. By default it requests all of them in one query, so **a request fails "
        "as soon as one of its semirings exhausts memory**; asking for a single semiring "
        "(`POST /api/v1/aps/explanation/{semiring}`) only pays for that one.")
    col_cmp, col_agg = st.columns(2)
    with col_cmp:
        st.markdown("#### Shape 1: every semiring but `boolexpr`")
        st.markdown(
            "`formula` and `why` exceed 3 GB from 22 readings in the compared group, `how` "
            "and `which` from 24–26: they grow more slowly, but just as exponentially. "
            "`boolexpr` and the probability (`probability_evaluate`) stay small: they keep "
            "the comparison as a compact circuit instead of expanding it.")
        wet = [c for c in curve if c["values"] ==
               "wet" and int(c["readings"]) >= 16]
        sizes = sorted({int(c["readings"]) for c in wet})
        cells = {(c["semiring"], int(c["readings"])): c for c in wet}
        st.dataframe(
            [{"Semiring": name.replace("probability_evaluate", "probability"),
              **{f"{n} readings": (_peak(cells[(name, n)]) if cells[(name, n)]["status"] == "ok"
                                   else "killed (3 GB)") if (name, n) in cells else ""
                 for n in sizes}}
             for name in _SEMIRING_ORDER if any((name, n) in cells for n in sizes)],
            width="stretch", hide_index=True)
        st.caption(
            "Peak memory on database `memtest`, `SUM(v) > 0.001` over n readings; *< 0.3 s* "
            "means the run ended before the first memory sample. Through ap-explanation, on "
            "Q295 (31 readings):")
        st.dataframe(
            [{"Request": f"/{r['semiring']}" if r["semiring"] != "all" else "all semirings",
              "Outcome": (f"killed at {format_mb(r['peak_backend_mb'])} after {r['seconds']} s"
                          if "server closed" in r["error"] else
                          f"ok, {format_mb(r['peak_backend_mb'])} in {r['seconds']} s")}
             for r in load_memory_semiring_service() if r["question_id"] == "532"]
            + [{"Request": "all semirings",
                "Outcome": _measured(load_memory_sweep().get("532"))}],
            width="stretch", hide_index=True)
    with col_agg:
        st.markdown("#### Shape 2: `formula` only")
        st.markdown(
            "ProvSQL evaluates an aggregate in the `formula` semiring only, so ap-explanation "
            "explains aggregate queries with `formula` alone: by default it returns `formula` "
            "and skips the others, and it rejects a request for any other semiring "
            "(`AggregateSemiringError`). Shape 2's cost is therefore `formula`'s, whatever "
            "is requested, and it cannot be avoided by choosing another semiring.")
        st.dataframe(
            [{"Request": "556 · " + (f"/{r['semiring']}" if r["semiring"] != "all" else "all semirings"),
              "Semirings returned": ", ".join(r["semirings_returned"]) or "—",
              "Outcome": (f"ok, {format_mb(r['peak_backend_mb'])} in {r['seconds']} s"
                          if r["status"] == "success" else f"rejected: {r['error']}")}
             for r in load_memory_semiring_service() if r["question_id"] == "556"],
            width="stretch", hide_index=True)
        st.caption("556 averages the 134,280 elevation points of Zurich's cell.")


def _render_memory_analysis() -> None:
    """The questions whose provenance can exhaust the database's memory, and why."""
    risk = load_memory_risk()
    sweep = load_memory_sweep()
    curve = load_memory_group_size()
    supported = [r for r in risk if r["supported"] == "true"]
    at_risk = [r for r in supported if r["verdict"]
               in ("exhausts", "heavy", "unknown")]

    st.markdown(
        "Two query shapes make ProvSQL build a provenance formula that outgrows the "
        "database's memory. Once it starts, nothing stops it: ProvSQL ignores "
        "`statement_timeout` and `pg_terminate_backend` while evaluating, so the run ends "
        "when the OOM killer takes the database backend down, with every session on it, or "
        "when the database is restarted.")

    _render_memory_setup()

    # A question can have both shapes: count it once, by its worst verdict.
    order = ["exhausts", "heavy", "unknown", "fine"]
    worst: dict[str, tuple[str, str]] = {}
    for r in risk:
        current = worst.get(r["question_id"])
        if current is None or order.index(r["verdict"]) < order.index(current[0]):
            worst[r["question_id"]] = (r["verdict"], r["supported"])
    counts = Counter(worst.values())

    c_ex, c_heavy, c_unsup = st.columns(3)
    c_ex.metric("Supported questions that exhaust memory", counts[("exhausts", "true")],
                help="Estimated above 3 GB for the database backend.")
    c_heavy.metric("Supported questions that are heavy", counts[("heavy", "true")],
                   help="Estimated between 100 MB and 3 GB.")
    c_unsup.metric("Unsupported questions with the same shapes",
                   counts[("exhausts", "false")] + counts[("heavy", "false")],
                   help="They would hit the same wall once ap-explanation supports them.")

    st.subheader("Why")
    col_cmp, col_join = st.columns(2)
    with col_cmp:
        st.markdown("#### 1. Comparison on a nested aggregate")
        st.markdown(
            "An aggregate computed in a CTE or subquery, then compared: *the last month with "
            "more than 1 mm of rain* sums each month's readings, then keeps the months whose "
            "sum exceeds 0.001 m. Whether a month passes depends on which of its readings "
            "exist, so ProvSQL keeps the comparison symbolic, and the `formula` semiring "
            "expands it over the **combinations of the group's readings**. Memory grows "
            "about **3× per reading in the compared group**, whatever their values: groups "
            "of mostly dry days cost the same.")
        st.code(extract_sql(load_ap("Q295")), language="sql")
        st.caption(
            "Measured on database `memtest` (3 GB limit), `formula` semiring, "
            "`SUM(v) > 0.001` over a group of n readings (`scripts/memtest_group_size.sh`):")
        st.dataframe(
            [{"Readings in the group": int(c["readings"]),
              "Values": "all non-zero" if c["values"] == "wet" else "4 in 5 at zero",
              "Formula size": f"{int(c['result_chars']):,} chars" if c["result_chars"] else "—",
              "Peak memory": _peak(c),
              "Time": f"{c['seconds']} s",
              "Outcome": "ok" if c["status"] == "ok" else f"{c['status']} (3 GB limit)"}
             for c in curve if c["semiring"] == "formula" and int(c["readings"]) >= 12],
            width="stretch", hide_index=True)
    with col_join:
        st.markdown("#### 2. Aggregate over many rows")
        st.markdown(
            "The formula of an aggregate lists every row it aggregates, about **3 KB of memory "
            "per row**. That is harmless over one cell's daily readings, but the rows pile up "
            "over the 134,280 elevation points of a cell, over a large region, or when readings "
            "are **joined to the elevation table**: each reading is then paired with every "
            "point of its cell the query keeps (about 2 KB per pair), and one month above "
            "500 m is 31 readings × 33,096 points = 1 million pairs. Memory follows the "
            "largest group of the aggregate rather than the total. Picking one point first "
            "(`ORDER BY elevation DESC LIMIT 1`, as the *highest point* questions do) avoids "
            "the pairs.")
        st.code(_ELEVATION_JOIN_EXAMPLE, language="sql")
        st.caption(
            "Measured through ap-explanation on the demo's data, 3 GB-capped database:")
        st.dataframe(
            [{"Question": f"{q} · {label}", "Rows in the formula": rows,
              "Measured": _measured(sweep.get(q)) if q in sweep else measured}
             for q, label, rows, measured in [
                 ("556", "average elevation of the cell", "134,280 points", None),
                 ("—", "same points, counted per 50 m band", "11 groups, 134,280 points",
                  "237 MB in 4.4 s"),
                 ("148", "one week above 500 m",
                  "7 × 33,096 = 231,672 pairs", None),
                 ("152", "one month above 500 m",
                  "31 × 33,096 = 1,025,976 pairs", None),
                 ("156", "2015–2016 above 500 m",
                  "731 × 33,096 = 24,193,176 pairs", None),
            ]],
            width="stretch", hide_index=True)

    _render_semirings(curve)

    st.subheader("Supported questions that may exhaust memory")
    st.caption(
        "Found by `scripts/memory_analysis.py` in the SQL of every question, and estimated "
        "from the measurements above, for the largest group: the cells of the region "
        "filter × the readings of the time unit (31-day months), × the elevation points "
        "kept, counted on Zurich's cell, the only elevation data at hand. Where measured, "
        "the last column gives the run's actual peak on the demo's data; a run stopped at "
        "180 s was still computing.")
    _risk_table(at_risk, sweep)
    fine = [r for r in supported if r["verdict"] == "fine"]
    if fine:
        st.caption(
            "Same shapes, but small enough to be fine: " + ", ".join(
                f"{r['question_id']} ({r['size_text'].split(' = ')[0]})" for r in fine) + ".")

    st.subheader("Measured on the demo's data")
    zurich_runs = [r for r in sweep.values() if r.get("sweep")]
    over = [r for r in zurich_runs if r["status"]
            != "success" or r["peak_backend_mb"] >= 100]
    risk_ids = {r["question_id"]
                for r in risk if r["verdict"] in ("exhausts", "heavy")}
    st.markdown(
        f"Every supported City of Zurich question the demo's data can answer "
        f"(**{len(zurich_runs)}**) was run through ap-explanation on a 3 GB-capped database "
        f"(`scripts/memtest_questions.py`). **{len(zurich_runs) - len(over)}** stayed under "
        f"100 MB. The **{len(over)}** others are all among the questions listed above"
        + ("." if all(r["question_id"] in risk_ids for r in over) else
           ", except " + ", ".join(r["question_id"] for r in over if r["question_id"] not in risk_ids) + "."))
    with st.expander("All measured runs"):
        st.dataframe(
            [{"Id": r["question_id"], "Outcome": _measured(r), "Rows": r["rows"],
              "Listed above": "yes" if r["question_id"] in risk_ids else ""}
             for r in sorted(zurich_runs, key=lambda r: -r["peak_backend_mb"])],
            width="stretch", hide_index=True)

    unsupported = [r for r in risk if r["supported"] !=
                   "true" and r["verdict"] in ("exhausts", "heavy")]
    n_unsupported = len({r["question_id"] for r in unsupported})
    with st.expander(f"Unsupported questions with the same shapes — {n_unsupported}"):
        st.markdown(
            "ap-explanation rejects these today (mostly `HAVING` and scalar subqueries). "
            "Supporting them as they are would reach the same limits: rewriting a `HAVING` as "
            "a `WHERE` on a nested `SELECT`, for one, produces shape 1.")
        _risk_table(unsupported, sweep)


# Row colours of the Summary, readable in light and dark themes (dark text on a light fill).
_STATUS_STYLE = {
    STATUS_OK: "background-color: #d3f0d6; color: #10361a",
    STATUS_MEMORY: "background-color: #fdf0b8; color: #3d3000",
    STATUS_UNSUPPORTED: "background-color: #f8d3d3; color: #4a1010",
}
_STATUS_ICON_BY = {STATUS_OK: "🟢", STATUS_MEMORY: "🟡", STATUS_UNSUPPORTED: "🔴"}


def _render_question_summary() -> None:
    """Every question of the set, coloured by whether it can be explained."""
    rows = question_statuses()
    counts = Counter(r["Status"] for r in rows)
    total = len(rows)

    st.markdown(
        f"The **{total}** questions of the meteo question set, and whether ap-explanation can "
        "explain them: 🟢 **runs** without issue, 🟡 **may exhaust memory** (estimated above "
        "the 3 GB limit, see *Memory analysis*), 🔴 **not supported** (rejected, see "
        "*Theoretically supported questions*, or failing at run time).")
    c_ok, c_mem, c_ko = st.columns(3)
    for col, status in ((c_ok, STATUS_OK), (c_mem, STATUS_MEMORY), (c_ko, STATUS_UNSUPPORTED)):
        col.metric(f"{_STATUS_ICON_BY[status]} {status}",
                   f"{counts[status]} ({counts[status] / total:.0%})")

    shown = st.multiselect(
        "Show", [STATUS_OK, STATUS_MEMORY, STATUS_UNSUPPORTED],
        default=[STATUS_OK, STATUS_MEMORY,
                 STATUS_UNSUPPORTED], key="summary_status",
        format_func=lambda s: f"{_STATUS_ICON_BY[s]} {s}")
    columns = ["Id", "Status", "Question", "Why", "Estimated memory", "Measured (3 GB cap)",
               "Category", "Tables"]
    table = pd.DataFrame(
        [r for r in rows if r["Status"] in shown], columns=columns)
    if table.empty:
        st.info("No question with the selected status.")
        return
    st.dataframe(
        table.style.apply(
            lambda row: [_STATUS_STYLE[row["Status"]]] * len(row), axis=1),
        width="stretch", hide_index=True, height=640,
        column_config={
            "Id": st.column_config.NumberColumn(width="small", format="%d"),
            "Status": st.column_config.TextColumn(width="medium"),
            "Category": st.column_config.TextColumn(width="small"),
            "Question": st.column_config.TextColumn(width="large"),
            "Why": st.column_config.TextColumn(width="large"),
        })
    st.caption(
        "🟢 also covers the questions estimated between 100 MB and 3 GB: they complete within "
        "the limit, and their estimate is in the *Estimated memory* column. Estimates come "
        "from the SQL (`scripts/memory_analysis.py`); *Measured* gives the actual peak where "
        "the question was run on the demo's data. A run-time failure measured on one question "
        "is applied to the other regions of the same template, whose SQL differs only by the "
        "region. Sort or search the table from its header and toolbar.")


tab_summary, tab_supported, tab_memory, tab_runner = st.tabs(
    ["📋 Summary", "📊 Theoretically supported questions", "🧠 Memory analysis",
     "🔎 Demo query runner"])

with tab_summary:
    _render_question_summary()

with tab_supported:
    _render_summary()

with tab_memory:
    _render_memory_analysis()

with tab_runner:
    st.caption(
        "NOTE : This is running some samples queries just to what provenance adds")
    selected = st.selectbox(
        "Question", _PRESET_LABELS, key="preset",
        help="Weather questions from the meteo question set, each packaged as an "
             "Analytical Pattern ap-explanation can run.")
    preset = _PRESET_BY_LABEL.get(selected)

    if preset:
        _render_question(preset)
    else:
        st.info("Pick a question above to see its SQL and run it.")
