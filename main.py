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
    PROVSQL_NEXT_BUILD,
    PROVSQL_NEXT_COMMIT,
    RUN_CURRENT,
    RUN_NEXT,
    RUN_NEXT_ADAPTED,
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
    load_memory_group_size_next,
    load_memory_risk,
    load_memory_semiring_service,
    load_memory_sweep,
    load_presets,
    load_provsql_checks,
    load_provsql_sweep,
    load_provsql_sweep_max_memory,
    question_statuses_next,
    SERVICE_DEADLINE_SECONDS,
    APX_PATCH_BRANCH,
    load_service_memory_runs,
    load_service_runs,
    service_outcome,
    load_question_set,
    load_recorded,
    question_statuses,
    reading_labels,
    run_outcome,
    seed_row_counts,
    shorten_formula,
    stamp_start_time,
    unsupported_reason,
    was_killed,
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
            "Live runs are disabled for this question: ProvSQL 1.12 evaluates its comparison on "
            "an aggregate by exhausting the database's memory, and ignores cancellation. "
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
        "an aggregate exhausts the database's memory (see *Memory analysis*). The reasons are "
        "those of ProvSQL 1.12; *ProvSQL 1.13* shows which of them the next version lifts.")


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
        "database's memory. Once it starts, nothing stops it: ProvSQL 1.12 ignores "
        "`statement_timeout` and `pg_terminate_backend` while evaluating, so the run ends "
        "when the OOM killer takes the database backend down, with every session on it, or "
        "when the database is restarted. ProvSQL 1.13 honours cancellation, refuses the "
        "first shape past a limit and stops at the memory budget it is given. Both shapes stay "
        "exponential; the comparison of 20 readings takes about half the memory it did (see "
        "*ProvSQL 1.13*).")

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


_OUTCOME_LABEL = {
    "ok": "✅ Explained",
    "empty": "⚪ Runs, empty answer",
    "wrong": "⚠️ Not the SQL answer",
    "limit": "⛔ Refused: world limit",
    "refused": "❌ Refused",
    "error": "❌ Fails",
    "timeout": "⌛ Stopped at 120 s",
    "killed": "🔴 Out of memory (3 GB)",
}
_RUN_LABEL = {RUN_CURRENT: "ProvSQL 1.12", RUN_NEXT: "ProvSQL 1.13, as sent today",
              RUN_NEXT_ADAPTED: "ProvSQL 1.13, adapted"}
_LOWEST_POINT_EXAMPLE = """SELECT elevation FROM pts
WHERE elevation = (SELECT MIN(elevation) FROM pts)
LIMIT 1"""
_ADAPTED_EXAMPLE = """SELECT y.*, sr_formula(provenance(), 'formula_mapping')
FROM (
  SELECT tmin - 273.15 AS celsius, latitude, longitude
  FROM meteo_tmin WHERE ...
  ORDER BY tmin
  LIMIT plain(10)                   -- was LIMIT 10
) AS y
WHERE present(provenance())         -- only the rows of the SQL answer"""


def _cost(run: dict) -> str:
    """Time and peak memory of a run; one under 0.1 s ends before the first memory sample."""
    memory = f", {format_mb(run['peak_backend_mb'])}" if run["peak_backend_mb"] else ""
    return f"{run['seconds']} s{memory}"


def _outcome_cell(run: dict | None, cost: bool = False) -> str:
    """Outcome of one query of the comparison, with its cost if asked."""
    if not run:
        return "—"
    outcome = run_outcome(run)
    text = _OUTCOME_LABEL[outcome]
    if outcome == "wrong":
        text += f": {run['rows']} row(s), SQL gives {run['plain_rows']}"
    if cost and outcome in ("ok", "empty", "wrong"):
        text += f" · {_cost(run)}"
    return text


def _outcome_counts(runs: list[dict]) -> str:
    counts = Counter(run_outcome(r) for r in runs)
    return " · ".join(f"{counts[o]} {label}" for o, label in _OUTCOME_LABEL.items() if counts[o])


def _check_cell(run: dict | None) -> str:
    """Outcome of one run-time check."""
    if not run:
        return "—"
    if run["check"] == "lowest_point" and run["status"] == "success":
        right = run["answer"] == run["plain_answer"]
        return (f"{'✅' if right else '⚠️'} returns {run['answer']} m"
                + ("" if right else f", SQL gives {run['plain_answer']} m"))
    if run["status"] == "success":
        return f"✅ {_cost(run)}"
    if run["status"] == "timeout":
        return f"⌛ cancelled after {run['seconds']} s"
    if was_killed(run):
        return f"🔴 killed at {format_mb(run['peak_backend_mb'])} after {run['seconds']} s"
    if run["sqlstate"] in ("54000", "0A000"):
        return f"⛔ refused in {run['seconds']} s: {run['error'][:80]}"
    return f"❌ fails: {run['error'][:80]}"


def _curve_cell(c: dict | None) -> str:
    if not c:
        return "—"
    if c["status"] == "ok":
        return f"{_peak(c)}, {c['seconds']} s"
    return "🔴 killed (3 GB)" if c["status"] == "killed" else f"⛔ refused in {c['seconds']} s"


_SERVICE_OUTCOME_LABEL = {
    "ok": "✅ SQL answer", "empty": "⚪ empty on the demo's data", "wrong": "❌ not the SQL answer",
    "apx": "⛔ refused by ap-explanation", "refused": "⛔ refused by ProvSQL", "limit": "🔢 world limit",
    "error": "💥 error", "stopped": "🛑 stopped by provsql.max_memory", "disk": "💽 stopped by temp_file_limit (3 GB)", "timeout": "⏱️ not finished",
    "killed": "🧨 database killed (3 GB)", "unchecked": "❔ plain SQL answer not computed",
}


def _render_service_check() -> None:
    """The questions sent to ap-explanation itself, unchanged, on ProvSQL 1.12 and 1.13."""
    questions = {q["question_id"]: q for q in load_question_set()}
    runs = {v: load_service_runs(v) for v in ("1.12", "1.13", "1.13 patched", "1.12 patched")}
    zurich = sorted(runs["1.13"], key=int)
    supported = [q for q in zurich if questions[q]["supported"] == "true"]
    outcome = {v: {q: service_outcome(r) for q, r in runs[v].items()} for v in runs}
    outcome_patched = outcome["1.13 patched"]

    st.subheader("Through ap-explanation itself")
    st.markdown(
        f"The runs above go through `scripts/provsql_compare.py`, which applies "
        f"ap-explanation's rewriter itself. To see what the demo would get, the City of "
        f"Zurich question of every template ({len(zurich)}, {len(supported)} of them "
        f"supported) was also sent to **ap-explanation v1.2.0, unchanged**, which annotates "
        f"the tables and asks for every semiring (`scripts/apx_service_check.py`), stopped "
        f"after {SERVICE_DEADLINE_SECONDS} s, on ProvSQL 1.12 and 1.13, and to ap-explanation "
        f"**patched** for ProvSQL 1.13 (branch `{APX_PATCH_BRANCH}`, below).")
    labels = list(_SERVICE_OUTCOME_LABEL)
    for title, ids in (("Supported by ap-explanation", supported),
                       ("Not supported by ap-explanation", [q for q in zurich if q not in supported])):
        counts = [Counter(outcome["1.12"][q] for q in ids), Counter(outcome["1.13"][q] for q in ids),
                  Counter(outcome_patched.get(q, "") for q in ids)]
        st.markdown(f"**{title}** ({len(ids)} templates)")
        st.dataframe(
            [{"Outcome": _SERVICE_OUTCOME_LABEL[o], "ProvSQL 1.12, as is": c12[o] or None,
              "ProvSQL 1.13, as is": c13[o] or None, "ProvSQL 1.13, patched": ca[o] or None}
             for o in labels for c12, c13, ca in [counts] if any(c[o] for c in counts)],
            width="stretch", hide_index=True)
    regressed = [q for q in supported if outcome["1.12"][q] == "ok" and outcome["1.13"][q] != "ok"]
    fixed = [q for q in regressed if outcome_patched.get(q) == "ok"]
    working = [q for q in supported if outcome["1.12"][q] == "ok"]
    kept = [q for q in working if outcome_patched.get(q) == "ok"]
    same_on_112 = sum(outcome["1.12 patched"].get(q) == outcome["1.12"][q] for q in outcome["1.12 patched"])
    wrong_before = [q for q in supported if outcome["1.12"][q] == "wrong"]
    by_kind = Counter(outcome["1.13"][q] for q in regressed)
    st.markdown(
        f"- **ProvSQL 1.13 breaks {len(regressed)} of the supported templates ap-explanation "
        f"answers on ProvSQL 1.12**, as it is: "
        + ", ".join(f"{n} {_SERVICE_OUTCOME_LABEL[o].split(' ', 1)[1]}" for o, n in by_kind.most_common())
        + ". They are the `ORDER BY … LIMIT k` and the comparisons with a subquery, which "
        f"ProvSQL 1.13 reads in every possible world. **Patched, {len(fixed)} of them return "
        f"the SQL answer again**: {len(kept)} of the {len(working)} supported templates that "
        f"work on ProvSQL 1.12 work on ProvSQL 1.13"
        + (f" (on ProvSQL 1.12, the patch leaves {same_on_112} of {len(outcome['1.12 patched'])} "
           f"outcomes unchanged)" if outcome["1.12 patched"] else "") + ".\n"
        f"- **{len(wrong_before)} supported templates do not return the SQL answer on ProvSQL "
        f"1.12** ({', '.join(questions[q]['category'] + questions[q]['template_index'] for q in wrong_before)}): "
        f"arithmetic on aggregates (a percentage, an anomaly) gives another value, 100 % read "
        f"as 10⁻¹², for one. ProvSQL 1.13 computes them right. The *Summary - ProvSQL 1.12* tab "
        f"counts them as not supported.\n"
        f"- **Annotating a large table outlasts ap-explanation's lock**: on the 41 million "
        f"elevation points of the memory runs below, the annotation took an hour and finished, "
        f"but the service reported a failure (*Cannot release a lock that's no longer owned*).")

    memory = load_service_memory_runs()
    if memory:
        kinds = Counter(service_outcome(r) for r in memory.values())
        killers = Counter(lost.split(",")[0] for r in memory.values() for lost in r["lost"])
        st.markdown(
            f"**With `provsql.max_memory = '1GB'`**, on the {len(memory)} questions that may "
            f"exhaust memory, each on the demo's data copied to every cell of its region: "
            + ", ".join(f"{n} {_SERVICE_OUTCOME_LABEL[o].split(' ', 1)[1]}" for o, n in kinds.most_common())
            + ". The processes the kernel killed: "
            + (", ".join(f"{who} ({n})" for who, n in killers.most_common()) or "none")
            + ". The budget covers the query's backend only: ProvSQL's circuit store "
              "(`ProvSQL MMap` worker) and PostgreSQL's parallel workers grow outside it, and "
              "the backend itself can pass it between two checks.")
        patched = load_service_memory_runs(patched=True)
        if patched:
            kinds = Counter(service_outcome(r) for r in patched.values())
            st.markdown(
                f"Sent to ap-explanation patched, the {len(patched)} of them it does not support "
                "as it is (33 for their `HAVING`), on the readings of their region: "
                + ", ".join(f"{n} {_SERVICE_OUTCOME_LABEL[o].split(' ', 1)[1]}" for o, n in kinds.most_common())
                + ": `provsql.max_worlds` refuses their comparisons on aggregates before memory "
                  "becomes a problem.")


def _render_provsql_next() -> None:
    """What ProvSQL 1.13 changes for the meteo questions, measured on its master branch."""
    sweep = load_provsql_sweep()
    checks = load_provsql_checks()
    questions = {q["question_id"]: q for q in load_question_set()}
    demo_ids = [p["id"] for p in _PRESETS]
    budgeted = load_provsql_sweep_max_memory()
    budget_check = checks["top_k_max_memory"]["1.13"]
    stopped = [q for q, r in budgeted.items() if "provsql.max_memory" in r["error"]]
    template_ids = [q for q in sweep[RUN_CURRENT] if q in questions]
    n = len(template_ids)
    per_template = len(questions) - sum(q["supported"] == "true" for q in questions.values())

    st.markdown(
        f"The demo runs **ProvSQL 1.12**. ProvSQL **1.13** is not released yet; its "
        f"development branch ({PROVSQL_NEXT_BUILD}) was built on the demo's database image and "
        f"given the same queries. It lifts most of the refusals behind the unsupported "
        f"questions, and stops instead of exhausting memory, **but only once ap-explanation "
        f"changes the queries it sends**: sent as they are today, three of the six demo "
        f"questions stop working.")

    st.subheader("How it was measured")
    st.markdown(
        f"The **{n}** City of Zurich questions that ProvSQL 1.12 does not support, one per "
        f"unsupported template (the {per_template} unsupported questions are these templates "
        f"asked of four regions), and the six demo questions, each run three times by "
        f"`scripts/provsql_compare.py`:\n"
        f"- **{_RUN_LABEL[RUN_CURRENT]}**, the query ap-explanation's rewriter produces;\n"
        f"- **{_RUN_LABEL[RUN_NEXT]}**, the same query;\n"
        f"- **{_RUN_LABEL[RUN_NEXT_ADAPTED]}**, with every `LIMIT k` written `LIMIT plain(k)` "
        f"and the rows filtered by `present(provenance())` (see *What ap-explanation has to "
        f"change*).\n\n"
        f"ap-explanation's own refusals (`HAVING`, aggregates outside its list) are lifted, to "
        f"see what ProvSQL does with the query. Each query runs with the `formula` semiring, in "
        f"a database of its own capped at 3 GB, annotated as ap-explanation annotates it, and "
        f"is stopped after 120 s. **Explained** means it ran and returned the rows plain SQL "
        f"returns.")

    counts = {run: Counter(run_outcome(sweep[run][q]) for q in template_ids) for run in _RUN_LABEL}
    columns = st.columns(3)
    for col, run in zip(columns, _RUN_LABEL):
        col.metric(f"Explained on {_RUN_LABEL[run]}", f"{counts[run]['ok']} of {n} templates",
                   help="Templates whose City of Zurich question ran and returned the plain SQL "
                        "answer, with at least one row.")
    empty = counts[RUN_NEXT_ADAPTED]["empty"]
    if empty:
        st.caption(
            f"{empty} more templates run on ProvSQL 1.13 with an empty answer on the demo's data "
            "(years or tables it lacks), so their provenance was not seen.")

    st.subheader("The unsupported questions, by reason")
    by_reason: dict[str, list[str]] = {}
    for q in template_ids:
        by_reason.setdefault(unsupported_reason(questions[q]["reason"])[0], []).append(q)
    st.dataframe(
        [{"Reason on ProvSQL 1.12": name, "Templates": len(ids),
          **{_RUN_LABEL[run]: _outcome_counts([sweep[run][q] for q in ids]) for run in _RUN_LABEL}}
         for name, ids in sorted(by_reason.items(), key=lambda item: -len(item[1]))],
        width="stretch", hide_index=True)
    st.markdown(
        "- **`ORDER BY` on an aggregate, `HAVING`, window functions**: ProvSQL 1.13 runs them, "
        "but for one `ORDER BY` template. A `LAG` value itself stays outside the provenance, "
        "with a warning.\n"
        "- **Scalar subquery reading a CTE**: accepted, but the *lowest / highest point* "
        "questions (`elevation = (SELECT min_elev …) LIMIT 1`) do not return the SQL answer, "
        "see *Still open*.\n"
        "- **Subquery in a condition**: accepted, then refused by the world limit or not "
        "finished in 120 s: `tmin = (SELECT MIN(tmin) …)` over a month is a comparison on an "
        "aggregate of 31 readings.\n"
        "- **`PERCENTILE_CONT`, `REGR_SLOPE`, `CORR`**: still not explained.\n"
        "- **`COUNT(DISTINCT …)` on an empty table**: fixed, see *Run-time problems*.")
    with st.expander(f"The {n} templates, one by one"):
        st.dataframe(
            [{"Id": int(q), "Question": questions[q]["question"],
              "Reason on ProvSQL 1.12": unsupported_reason(questions[q]["reason"])[0],
              **{_RUN_LABEL[run]: _outcome_cell(sweep[run][q]) for run in _RUN_LABEL},
              "Detail (1.13, adapted)": sweep[RUN_NEXT_ADAPTED][q]["error"]
              or "; ".join(sweep[RUN_NEXT_ADAPTED][q]["warnings"])}
             for q in template_ids],
            width="stretch", hide_index=True,
            column_config={"Id": st.column_config.NumberColumn(width="small", format="%d")})
        st.caption(
            "On ProvSQL 1.12 a few templates ap-explanation refuses do run once its refusal is "
            "lifted, some with another answer than plain SQL. *Detail* is ProvSQL's error, or "
            "its warnings about the parts of the query it leaves out of the provenance.")

    st.subheader("The six demo questions")
    st.dataframe(
        [{"Question": f"{p['id']} · {p['question']}",
          **{_RUN_LABEL[run]: _outcome_cell(sweep[run].get(p["id"]), cost=True) for run in _RUN_LABEL}}
         for p in _PRESETS if p["id"] in demo_ids],
        width="stretch", hide_index=True)
    st.markdown(
        "ProvSQL 1.13 reads `ORDER BY … LIMIT k` **in every possible world**: the result holds "
        "every row that may be among the first *k*, each with the condition for it to be. That "
        "condition involves all the other rows, so G81's ten lowest of 31 readings gives a "
        "formula of over 1 GB, which ProvSQL 1.13 now refuses (the first build measured ran "
        "out of memory at 3 GB), and C155 and T157, which rank readings joined to the "
        "elevation points, do not finish. `LIMIT plain(k)` asks for the truncation of the actual result, as "
        "ProvSQL 1.12 did. There is no setting for it.")

    _render_service_check()

    st.subheader("What ap-explanation has to change")
    col_text, col_code = st.columns(2)
    with col_text:
        st.markdown(
            "1. **Write every `LIMIT k` as `LIMIT plain(k)`**, or the query is ranked in every "
            "possible world (above).\n"
            "2. **Keep the rows where `present(provenance())`**. ProvSQL 1.13 can return rows "
            "that are not in the SQL answer, each with the condition under which it would be: "
            "a group failing its `HAVING`, a row failing a comparison with a subquery.\n"
            "3. **Stop refusing `HAVING`** in its rewriter.\n"
            "4. **Match errors on their new form**: a refusal is now SQLSTATE `0A000` instead "
            "of `XX000`, with its cause on a `DETAIL` line (`provsql-reason: …`), so the error "
            "texts the classification of this demo relies on change.")
        st.markdown(
            f"1 to 3 are done on ap-explanation's branch `{APX_PATCH_BRANCH}`, only when the "
            "database runs ProvSQL 1.13 or later (the repository reads the installed version): "
            "see *Through ap-explanation itself* for what it changes.")
        st.caption(
            "A database annotated under ProvSQL 1.12 keeps working after "
            "`ALTER EXTENSION provsql UPDATE`, which the database image runs at start.")
    with col_code:
        st.code(_ADAPTED_EXAMPLE, language="sql")
        st.caption("G81 as the *adapted* run sends it.")

    st.subheader("Run-time problems")
    st.dataframe(
        [{"Check": runs[next(iter(runs))]["what"],
          "ProvSQL 1.12": _check_cell(runs.get("1.12")), "ProvSQL 1.13": _check_cell(runs.get("1.13"))}
         for runs in checks.values()],
        width="stretch", hide_index=True)
    st.markdown(
        "- **A comparison on an aggregate no longer takes the database down.** ProvSQL 1.13 "
        "refuses it once the group has more than `provsql.max_worlds` possible worlds (2²⁰ by "
        "default, that is 20 readings). Under that it costs what it did: memory still grows "
        "exponentially with the group (table below), so Q295 gets an error instead of an "
        "answer.\n"
        "- **Cancellation works**: `statement_timeout` and a cancel now stop an evaluation.\n"
        "- **Aggregates over many rows cost the same**, and the join with the elevation points "
        "fits in the 3 GB limit, as it does on ProvSQL 1.12. The first 1.13 build measured "
        "went past the limit on it.\n"
        f"- **`provsql.max_memory`**, the new memory budget, now stops the top-10 of 31 "
        f"readings: set to 1 GB it ends the evaluation at {format_mb(budget_check['peak_backend_mb'])}, "
        f"where the backend was killed at 3 GB without it. On the first build measured neither "
        f"`provsql.max_memory` nor `max_worlds` stopped it; it was fixed after being reported "
        f"as [provsql#104](https://github.com/PierreSenellart/provsql/issues/104). A text "
        f"result over 1 GB, the most PostgreSQL holds, is now refused with error 54000 "
        f"instead of an internal error. Over the {len(budgeted)} questions, a 1 GB budget "
        f"killed {sum(map(was_killed, budgeted.values()))} backends and stopped "
        f"{len(stopped)} evaluation{'s' if len(stopped) != 1 else ''} "
        f"({', '.join(stopped) or 'none'}). It stays approximate, see *Still open*.")
    current = {(c["semiring"], c["values"], int(c["readings"])): c for c in load_memory_group_size()}
    upcoming = {(c["semiring"], c["values"], int(c["readings"])): c for c in load_memory_group_size_next()}
    sizes = sorted({k[2] for k in (*current, *upcoming) if k[:2] == ("formula", "wet") and k[2] >= 16})
    st.dataframe(
        [{"Readings in the compared group": size,
          "ProvSQL 1.12": _curve_cell(current.get(("formula", "wet", size))),
          "ProvSQL 1.13": _curve_cell(upcoming.get(("formula", "wet", size)))}
         for size in sizes],
        width="stretch", hide_index=True)
    st.caption(
        "`formula` semiring, `SUM(v) > 0.001` over a group of n readings, 3 GB limit "
        "(`scripts/memtest_group_size.sh`); *< 0.3 s* means the run ended before the first "
        "memory sample. `why`, `how` and `which` are refused at the same size; `boolexpr` and "
        "the probability stay small, as on ProvSQL 1.12.")

    st.subheader("Still open on ProvSQL 1.13")
    lowest = checks.get("lowest_point", {})
    runs_next = (sweep[RUN_NEXT], sweep[RUN_NEXT_ADAPTED])
    largest = max((r for run in runs_next for r in run.values()), key=lambda r: r["store_growth_mb"])
    store_lost = [lost for run in runs_next for lost in [run[largest["question_id"]]["lost"]]]
    over_budget = max((r for r in budgeted.values() if r["status"] == "success"),
                      key=lambda r: r["peak_backend_mb"])
    col_bug, col_rest = st.columns(2)
    with col_bug:
        st.markdown("#### A row that is not the SQL answer")
        st.code(_LOWEST_POINT_EXAMPLE, language="sql")
        st.markdown(
            "Over the 8 lowest elevation points of the cell, stored highest first, plain SQL "
            f"returns **{lowest['1.13']['plain_answer']} m**, ProvSQL 1.13 "
            f"**{lowest['1.13']['answer']} m** and ProvSQL 1.12 **{lowest['1.12']['answer']} m**. "
            "The comparison keeps every point, each with the condition for it to be the "
            "lowest, and a `LIMIT` without `ORDER BY` then takes one of them, whether it is "
            "the lowest or not; `LIMIT plain(1)` does the same. ProvSQL 1.12 never got that "
            "far on the *lowest / highest point* templates, which reach the comparison "
            "through CTEs it loses. ProvSQL 1.13 runs them: as sent today they return another "
            "point, and filtered by `present(provenance())` no row at all.")
    with col_rest:
        st.markdown("#### Memory")
        st.markdown(
            "- **`provsql.max_memory` is a best-effort limit**, checked in the evaluation "
            "loops, every 50 ms and after each large formula, not while the possible worlds of "
            "a comparison are listed: with `provsql.max_worlds = 0`, the months of 2019 "
            "(28 to 31 readings) are still killed at 3 GB under a 1 GB budget"
            f", and question {over_budget['question_id']} runs to the end at "
            f"{format_mb(over_budget['peak_backend_mb'])} under the same budget.\n"
            "- **The circuit store grows with every query, and a stopped query leaves its "
            f"part behind.** Question {largest['question_id']} alone added "
            f"{format_mb(largest['store_growth_mb'])} to it"
            + (", and the kernel killed ProvSQL's store worker" if any(
                "background worker" in lost for lost_run in store_lost for lost in lost_run) else "")
            + ". The store is shared by the whole database: once it outgrows the memory, "
            "every provenance query on that database fails. ProvSQL 1.12 has the same store, "
            "but refused the queries that grow it this much.\n"
            "- **Comparisons on an aggregate stay exponential**; the world limit only makes "
            "them fail early.")

    st.subheader("What was not measured")
    st.markdown(
        "- **The other regions.** Only the City of Zurich question of each template ran. Regions "
        "of several cells have larger groups, so more of their comparisons on an aggregate "
        "(`HAVING` included) will meet the world limit.\n"
        "- **The supported questions**, apart from the six of the demo: any of them with an "
        "`ORDER BY … LIMIT` needs `LIMIT plain(k)`.\n"
        "- **Tables the demo's data lacks**: `meteo_tmean`, `meteo_sdmax`, `meteo_ssrd` and the "
        "land cover tables are stand-ins derived from the readings at hand "
        "(`scripts/provsql_compare_setup.sql`), enough to run the queries.\n"
        "- **The other semirings**, apart from the comparison on an aggregate above.")


# Row colours of the Summary, readable in light and dark themes (dark text on a light fill).
_STATUS_STYLE = {
    STATUS_OK: "background-color: #d3f0d6; color: #10361a",
    STATUS_MEMORY: "background-color: #fdf0b8; color: #3d3000",
    STATUS_UNSUPPORTED: "background-color: #f8d3d3; color: #4a1010",
}
_STATUS_ICON_BY = {STATUS_OK: "🟢", STATUS_MEMORY: "🟡", STATUS_UNSUPPORTED: "🔴"}


def _status_metrics(rows: list[dict], key: str = "Status", label: str = "") -> None:
    counts, total = Counter(r[key] for r in rows), len(rows)
    for col, status in zip(st.columns(3), (STATUS_OK, STATUS_MEMORY, STATUS_UNSUPPORTED)):
        col.metric(f"{label}{_STATUS_ICON_BY[status]} {status}", f"{counts[status]} ({counts[status] / total:.0%})")


def _render_question_summary(next_version: bool = False) -> None:
    """Every question of the set, coloured by whether it can be explained, on ProvSQL 1.12
    (the demo) or, with *next_version*, on ProvSQL 1.13 through ap-explanation as it is."""
    if next_version:
        rows = question_statuses_next()
    else:
        on_next = {r["Id"]: f"{_STATUS_ICON_BY[r['Status']]} {r['Status']}" for r in question_statuses_next()}
        rows = [r | {"With ProvSQL 1.13": on_next[r["Id"]]} for r in question_statuses()]
    total = len(rows)

    st.markdown(
        f"The **{total}** questions of the meteo question set, and whether ap-explanation can "
        "explain them: 🟢 **runs** without issue, 🟡 **may exhaust memory** (estimated above "
        "the 3 GB limit, see *Memory analysis*), 🔴 **not supported** (rejected, see "
        "*Theoretically supported questions*, or failing at run time)."
        + (f" This tab is the same table on **ProvSQL 1.13** ({PROVSQL_NEXT_BUILD}), measured "
           "**through ap-explanation v1.2.0 itself, unchanged**: the service annotates the "
           "tables and asks for every semiring, as the demo does."
           if next_version else ""))
    if next_version:
        _status_metrics(rows, label="As is: ")
        _status_metrics(rows, key="Patched", label="Patched: ")
        st.caption(
            "*As is*: ap-explanation v1.2.0 as deployed. *Patched*: its rewriter adapted to "
            f"ProvSQL 1.13 (branch `{APX_PATCH_BRANCH}`): every `LIMIT k` sent as "
            "`LIMIT plain(k)`, the rows kept where `present(provenance())`, `HAVING` no longer "
            "refused; unchanged before ProvSQL 1.13 (see *ProvSQL 1.13*).")
        blocked = Counter((r["Blocked by"], r["Fix"]) for r in rows if r["Status"] != STATUS_OK)
        st.markdown("**What the 🟡 and 🔴 questions are blocked by**, and what would remove it:")
        st.dataframe(pd.DataFrame([{"Blocked by": b or "—", "Fix": f or "—", "Questions": n}
                                   for (b, f), n in blocked.most_common()]),
                     width="stretch", hide_index=True)
    else:
        _status_metrics(rows)

    shown = st.multiselect(
        "Show", [STATUS_OK, STATUS_MEMORY, STATUS_UNSUPPORTED],
        default=[STATUS_OK, STATUS_MEMORY,
                 STATUS_UNSUPPORTED], key="summary_status_next" if next_version else "summary_status",
        format_func=lambda s: f"{_STATUS_ICON_BY[s]} {s}")
    columns = (["Id", "Status", "Question", "Why", "Patched", "Blocked by", "Fix", "On ProvSQL 1.12",
                "Measured on", "Measured (3 GB cap)", "Estimated memory", "Category", "Tables"]
               if next_version else
               ["Id", "Status", "Question", "Why", "With ProvSQL 1.13", "Estimated memory",
                "Measured (3 GB cap)", "Category", "Tables"])
    table = pd.DataFrame([r for r in rows if r["Status"] in shown], columns=columns)
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
    if next_version:
        st.caption(
            "Each template was run on its City of Zurich question (the demo's data covers that "
            "cell only), and the other regions of the template are given its outcome: their "
            "SQL differs only by the region. The questions that may exhaust memory were run "
            "themselves, on the demo's data copied to every cell of their region (what the "
            "memory estimates assume), with `provsql.max_memory = '1GB'` set on the database: "
            "the service does not set it, and it is unlimited by default. A question that runs "
            "with an empty answer on the demo's data keeps its ProvSQL 1.12 status: 🟢 if it "
            "ran there, 🔴 otherwise (its provenance was not seen). *Measured on* is the "
            "question run, *Measured* its peak backend memory, outcome and time "
            f"({SERVICE_DEADLINE_SECONDS} s at most). Runs from `scripts/apx_service_check.py`, "
            "with ap-explanation as is and patched.")
    st.caption(
        "🟢 also covers the questions estimated between 100 MB and 3 GB: they complete within "
        "the limit, and their estimate is in the *Estimated memory* column. Estimates come "
        "from the SQL (`scripts/memory_analysis.py`); *Measured* gives the actual peak where "
        "the question was run on the demo's data. A run-time failure measured on one question "
        "is applied to the other regions of the same template, whose SQL differs only by the "
        "region."
        + ("" if next_version else
           " A template whose City of Zurich question, sent to ap-explanation, does not "
           "return the plain SQL answer is 🔴. *With ProvSQL 1.13* is the question's status "
           "on ProvSQL 1.13 with ap-explanation as it is (see the next tab).")
        + " Sort or search the table from its header and toolbar.")


tab_summary, tab_summary_next, tab_supported, tab_memory, tab_next, tab_runner = st.tabs(
    ["📋 Summary - ProvSQL 1.12", f"📋 Summary - ProvSQL 1.13 (master at {PROVSQL_NEXT_COMMIT})",
     "📊 Theoretically supported questions", "🧠 Memory analysis",
     "🆕 ProvSQL 1.13", "🔎 Demo query runner"])

with tab_summary:
    _render_question_summary()

with tab_summary_next:
    _render_question_summary(next_version=True)

with tab_supported:
    _render_summary()

with tab_memory:
    _render_memory_analysis()

with tab_next:
    _render_provsql_next()

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
