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
    APX_114_COMMIT,
    APX_FORMULA_ROWS_COMMIT,
    APX_MASTER_COMMIT,
    PROVSQL_114_BUILD,
    PROVSQL_MASTER_BUILD,
    PROVSQL_MASTER_COMMIT,
    PROVSQL_NEXT_BUILD,
    RUN_NEXT,
    RUN_NEXT_ADAPTED,
    SERVICE_DEADLINE_SECONDS,
    STATUS_CRASH,
    STATUS_CAVEAT,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    ap_tables,
    cited_readings,
    database_name,
    extract_sql,
    format_mb,
    format_reading,
    load_ap,
    load_memory_risk,
    load_presets,
    load_provsql_checks,
    load_provsql_sweep,
    load_provsql_sweep_max_memory,
    load_question_set,
    load_recorded,
    load_service_memory_runs,
    question_statuses,
    question_statuses_next,
    reading_labels,
    service_outcome,
    shorten_formula,
    stamp_start_time,
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


# Row colours of the summaries, readable in light and dark themes (dark text on a light fill).
_STATUS_STYLE = {
    STATUS_OK: "background-color: #d3f0d6; color: #10361a",
    STATUS_CAVEAT: "background-color: #fdf0b8; color: #3d3000",
    STATUS_CRASH: "background-color: #fbd9ae; color: #4a2700",
    STATUS_UNSUPPORTED: "background-color: #f8d3d3; color: #4a1010",
}
_STATUS_ICON_BY = {STATUS_OK: "🟢", STATUS_CAVEAT: "🟡", STATUS_CRASH: "🟠", STATUS_UNSUPPORTED: "🔴"}


def _render_question_summary(version: str = "1.12") -> None:
    """Every question of the set, coloured by whether ap-explanation can explain it: v1.2.0
    on ProvSQL 1.12 (the demo), or master on ProvSQL 1.13, 1.14 or 1.14.0 (*version*)."""
    next_version = version != "1.12"
    rows = question_statuses_next(version) if next_version else question_statuses()
    total = len(rows)

    if version == "1.13":
        st.markdown(
            f"The **{total}** questions of the meteo question set on **ProvSQL 1.13** "
            f"({PROVSQL_NEXT_BUILD}), sent to **ap-explanation master** (`{APX_MASTER_COMMIT}`), "
            "whose rewriter is adapted to ProvSQL 1.13: every `LIMIT k` is sent as "
            "`LIMIT plain(k)`, the rows are kept where `present(provenance())`, and `HAVING` is "
            "accepted. The City of Zurich question of each template ran with "
            "`provsql.max_memory = '3GB'` and `statement_timeout = '180s'`; the questions estimated "
            "to exhaust memory ran themselves, on their region's data, with "
            "`provsql.max_memory = '1GB'`.")
    elif version == "1.14":
        st.markdown(
            f"The **{total}** questions of the meteo question set on **ProvSQL 1.14** "
            f"({PROVSQL_MASTER_BUILD}), sent to **ap-explanation** `feat/formula-rows` "
            f"(`{APX_FORMULA_ROWS_COMMIT}`): master, with the pre-check that refuses an aggregate "
            "over more than 8,000,000 rows or whose plain SQL takes over 60 s, and an aggregate's "
            "formula read as its first 100 rows with ProvSQL's `sr_formula_rows`. It runs each "
            "query with `provsql.max_memory = '1GB'`, `statement_timeout = '180s'` and no parallel "
            "workers. The City of Zurich question of each template ran on the demo's data; the "
            "questions estimated to exhaust memory ran on their region's data: those of Attica on "
            "this build (the demo's data copied to Attica's 24 cells), those of the Canton of Bern "
            "and the Peloponnese on ProvSQL 1.13.0 only, and keep that outcome.")
        st.markdown(
            "On this build, no question run crashes the database, on the demo's data or on "
            "Attica's: 156, 388 and 392 (24 and 49 million aggregated rows) are refused before they "
            "run, 384, which ProvSQL's `sr_formula` could not stop, is answered in 43 s, and the "
            "Attica questions that crashed on 1.13.0 are answered or refused. The 🟠 left are "
            "questions of the Canton of Bern and the Peloponnese, measured on 1.13.0 only. The "
            "pre-check has a cost: the plain SQL of 301 on Attica's data takes over 60 s, while its "
            "provenance query answered in 0.3 s, and it is refused.")
    elif version == "1.14.0":
        st.markdown(
            f"The **{total}** questions of the meteo question set on **ProvSQL {PROVSQL_114_BUILD}**, "
            f"sent to **ap-explanation master** (`{APX_114_COMMIT}`, the coming 2.0.0, which requires "
            "it): the same pre-check and `sr_formula_rows` as the 1.14 master tab, run with the same "
            "settings (`provsql.max_memory = '1GB'`, `statement_timeout = '180s'`, pre-check 60 s), "
            "on the City of Zurich questions and on the Attica ones (the demo's data copied to "
            "Attica's 24 cells); the Canton of Bern and Peloponnese ones keep their 1.13.0 outcome.")
        st.markdown(
            "Nothing changes from the master@3b8d3098 tab: every question has the same outcome, and "
            "every answered one the same rows and the same formulas (`formula_digest`). ProvSQL "
            "1.14.0 adds no function over 3b8d3098 (in effect 1.13.1): it evaluates *probabilities* "
            "faster, splitting a lineage into independent parts and caching the d-DNNFs it compiles, "
            "and the demo asks for none. Run one at a time on both builds with the same "
            "ap-explanation, the slowest questions take the same time, within 10%. Six questions "
            "that `statement_timeout` stops at 180 s were measured with a 300 s deadline: the "
            "service answers a few seconds after the statement is stopped.")
    else:
        st.markdown(
            f"The **{total}** questions of the meteo question set on **ProvSQL 1.12**, the "
            "demo's, sent to **ap-explanation v1.2.0**.")
    st.markdown(
        "🟢 **Supported**: it returns the plain SQL answer with its provenance. 🟡 **Supported, "
        "with caveats**: it runs, but ProvSQL stops it for lack of memory or time (`provsql.max_memory`, "
        f"`temp_file_limit`, the {SERVICE_DEADLINE_SECONDS} s time limit), ap-explanation refuses it "
        "beforehand for the same reason (too many aggregated rows, or plain SQL too slow), or the "
        "demo's data has no row for it (see *Memory*). 🟠 **Runtime crash**: nothing stops it: it exhausts the memory and "
        "the kernel kills a ProvSQL process, taking every session down (measured on ProvSQL 1.13, "
        "estimated on 1.12, which has no budget), or it runs on past `statement_timeout`. 🔴 **Not supported**: refused, failing, or not "
        "returning the plain SQL answer.")

    counts = Counter(r["Status"] for r in rows)
    for col, status in zip(st.columns(4), (STATUS_OK, STATUS_CAVEAT, STATUS_CRASH, STATUS_UNSUPPORTED)):
        col.metric(f"{_STATUS_ICON_BY[status]} {status}", f"{counts[status]} ({counts[status] / total:.0%})")

    if next_version:
        blocked = Counter(r["Blocked by"] for r in rows if r["Status"] != STATUS_OK)
        st.markdown("**What the 🟡 and 🔴 questions are blocked by:**")
        st.dataframe(pd.DataFrame([{"Blocked by": b, "Questions": n, "Meaning": _BLOCKED_MEANING.get(b, "")}
                                   for b, n in blocked.most_common()]),
                     width="stretch", hide_index=True)

    shown = st.multiselect(
        "Show", [STATUS_OK, STATUS_CAVEAT, STATUS_CRASH, STATUS_UNSUPPORTED],
        default=[STATUS_OK, STATUS_CAVEAT, STATUS_CRASH, STATUS_UNSUPPORTED],
        key=f"summary_status_{version}",
        format_func=lambda s: f"{_STATUS_ICON_BY[s]} {s}")
    columns = (["Id", "Status", "Question", "Why", "Blocked by",
                *(["On ProvSQL 1.13"] if version.startswith("1.14") else []), "On ProvSQL 1.12", "Measured on",
                "Measured (3 GB cap)", "Estimated memory", "Category", "Tables"]
               if next_version else
               ["Id", "Status", "Question", "Why", "Estimated memory", "Measured (3 GB cap)",
                "Category", "Tables"])
    table = pd.DataFrame([r for r in rows if r["Status"] in shown], columns=columns)
    if table.empty:
        st.info("No question with the selected status.")
        return
    st.dataframe(
        table.style.apply(lambda row: [_STATUS_STYLE[row["Status"]]] * len(row), axis=1),
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
            "cell only), and its other regions are given that outcome: their SQL differs only by "
            "the region. The questions estimated to exhaust memory were run themselves, on the demo's "
            "data copied to every cell of their region. *Measured* is "
            f"the peak backend memory, outcome and time ({SERVICE_DEADLINE_SECONDS} s at most), "
            "from `scripts/apx_service_check.py`.")
    else:
        st.caption(
            "Estimates come from the SQL (`scripts/memory_analysis.py`); *Measured* is the actual "
            "peak where the question was run on the demo's data. A run-time failure measured on "
            "one question is applied to the other regions of its template. A template whose City "
            "of Zurich question, sent to ap-explanation, does not return the plain SQL answer is 🔴.")


_BLOCKED_MEANING = {
    "ProvSQL limit": "What ProvSQL 1.13 does not support: PERCENTILE_CONT, REGR_SLOPE and CORR, a LIMIT "
                     "in a CTE, comparisons on aggregates past provsql.max_worlds. ap-explanation "
                     "cannot change it.",
    "Memory / time": "Stopped by a limit of memory or time (🟡), or killed at 3 GB "
                     "(🟠) (see *Memory*).",
    "ProvSQL bug": "A query ProvSQL should evaluate but fails on.",
    "The demo's data": "Supported, but the demo's data has no row for it: its provenance was not seen.",
    "The question's SQL": "Empty in plain SQL too.",
    "ap-explanation": "Refused by ap-explanation's rewriter.",
}

# Memory: the questions that may exhaust memory, run through ap-explanation on ProvSQL 1.13.
_MEMORY_OUTCOME = {
    "killed": "🧨 Database killed at 3 GB",
    "timeout": f"⏱️ Still computing after {SERVICE_DEADLINE_SECONDS} s",
    "stopped": "🛑 Stopped by provsql.max_memory",
    "disk": "💽 Stopped by temp_file_limit",
    "limit": "⛔ Refused by provsql.max_worlds",
    "ok": "✅ Answered",
    "unchecked": "❔ Answered, not compared with plain SQL",
}
_PROBLEMS = {
    "comparison": (
        "A comparison on an aggregate",
        "`HAVING COUNT(*) >= 3`, `= (SELECT MAX(…))`, a month whose `SUM` exceeds 1 mm. Whether a "
        "group passes depends on which of its rows exist, so ProvSQL considers every combination "
        "of them: memory triples with each row of the group.",
        "Database killed from about 22 rows in the group.",
        "Mostly refused in seconds, past 20 rows in the group (`provsql.max_worlds`).",
    ),
    "large aggregate": (
        "An aggregate over many rows",
        "The average or highest elevation of a region, or readings joined to the elevation "
        "points. The provenance lists every row aggregated, about 2–3 KB each, and a region "
        "holds millions of elevation points.",
        "Not measured on the regions' data.",
        "Mostly killed at 3 GB or still computing, despite the 1 GB budget.",
    ),
}


def _render_memory() -> None:
    """The questions whose provenance can exhaust memory, and whether ProvSQL 1.13's budget holds."""
    runs = load_service_memory_runs()
    questions = {q["question_id"]: q for q in load_question_set()}
    shape: dict[str, str] = {}
    for r in sorted(load_memory_risk(), key=lambda r: r["estimated_mb"]):
        shape[r["question_id"]] = r["shape"]  # the largest estimate last, so it wins
    outcome = {q: service_outcome(r) for q, r in runs.items()}

    st.markdown(
        f"The **{len(runs)}** questions estimated to exhaust memory were sent to ap-explanation "
        "on ProvSQL 1.13, each on the demo's data copied to every cell of its region. The "
        "database is capped at **3 GB of RAM** (no swap) and has **`provsql.max_memory = '1GB'`**, "
        "ProvSQL 1.13's per-query budget, unlimited by default.")
    counts = Counter(outcome.values())
    for col, o in zip(st.columns(len(_MEMORY_OUTCOME)), _MEMORY_OUTCOME):
        col.metric(_MEMORY_OUTCOME[o], counts[o])

    st.subheader("What makes a question exhaust memory")
    st.dataframe(
        [{"Problem": name, "Example": example,
          "Questions": sum(shape.get(q) == key for q in runs),
          "ProvSQL 1.12": before, "ProvSQL 1.13, 1 GB budget": after,
          "Outcomes on ProvSQL 1.13": ", ".join(
              f"{n} × {_MEMORY_OUTCOME[o].split(' ', 1)[1]}"
              for o, n in Counter(outcome[q] for q in runs if shape.get(q) == key).most_common())}
         for key, (name, example, before, after) in _PROBLEMS.items()],
        width="stretch", hide_index=True,
        column_config={"Example": st.column_config.TextColumn(width="large")})
    st.markdown(
        "A third problem is fixed: ProvSQL 1.13 reads `ORDER BY … LIMIT k` in every possible world, "
        "and the ten lowest of 31 readings then give a formula of over 1 GB. ap-explanation master "
        "sends `LIMIT plain(k)`, the truncation of the actual answer.")

    st.subheader("Does the 1 GB budget hold? Not for these queries")
    killed = [q for q in runs if outcome[q] == "killed"]
    by_process = Counter(runs[q]["lost"][0].split(",")[0] for q in killed)
    computing = [runs[q] for q in runs if outcome[q] == "timeout"]
    over = [r for r in computing if r["peak_backend_mb"] > 1024]
    budgeted = load_provsql_sweep_max_memory()
    passed = [r for r in budgeted.values() if r["status"] == "success" and r["peak_backend_mb"] > 1024]
    checks = load_provsql_checks()
    st.markdown(
        f"- **{len(killed)} questions took the database down** (the kernel killed a process at "
        "3 GB, ending every session): "
        + ", ".join(f"{_PROCESS.get(p, p)} {n}×" for p, n in by_process.most_common())
        + ". The budget counts the query's own process only, which was at 0.4 to 1 GB when the "
          "database died: the rest of the 3 GB was ProvSQL's circuit store and PostgreSQL's "
          "parallel workers, which the budget does not cover.\n"
        f"- **{len(computing)} were still computing after {SERVICE_DEADLINE_SECONDS} s**, without "
        "error"
        + (f"; {', '.join(r['question_id'] for r in over)} already above the budget, at "
           f"{', '.join(format_mb(r['peak_backend_mb']) for r in over)}" if over else "")
        + ". ap-explanation sets no `statement_timeout`: they run until they finish or the "
          "database dies, holding ap-explanation's lock on the database (1 h).\n"
        f"- **{counts['stopped']} were stopped by the budget**, as it should be, and "
        f"{counts['disk']} by its temporary files (see *Disk*).\n"
        + "".join(f"- Question {r['question_id']} ({questions[r['question_id']]['question']}) "
                  f"**finished at {format_mb(r['peak_backend_mb'])}** under the same budget.\n"
                  for r in passed if r["question_id"] in questions)
        + f"- With `provsql.max_worlds = 0`, a comparison on an aggregate is **killed at "
          f"{format_mb(checks['comparison_unlimited']['1.13']['peak_backend_mb'])}** under a 1 GB budget: "
          "ProvSQL does not check the budget while it lists the possible worlds. The default "
          "`max_worlds` refuses it first.")

    st.subheader("The questions, one by one")
    order = list(_MEMORY_OUTCOME)
    st.dataframe(
        [{"Id": int(q), "Question": questions[q]["question"],
          "Problem": _PROBLEMS[shape[q]][0] if q in shape else "",
          "Outcome": _MEMORY_OUTCOME.get(outcome[q], outcome[q]),
          "Peak of the query's process": format_mb(runs[q]["peak_backend_mb"]),
          "Process killed": ", ".join(_PROCESS.get(p.split(",")[0], p) for p in runs[q]["lost"]),
          "Seconds": runs[q]["seconds"]}
         for q in sorted(runs, key=lambda q: (order.index(outcome[q]) if outcome[q] in order else 99, int(q)))],
        width="stretch", hide_index=True, height=480,
        column_config={"Id": st.column_config.NumberColumn(width="small", format="%d")})
    st.caption(
        "From `scripts/apx_service_check.py`: ap-explanation v1.2.0, and for the questions it "
        "refuses (`HAVING`) its adapted rewriter, now in master. *Peak* is the query's backend, "
        "sampled every 0.1 s.")


_PROCESS = {
    'background worker "ProvSQL MMap"': "ProvSQL's circuit store worker",
    'background worker "parallel worker"': "a parallel worker",
    "server process": "the query's own process",
}


def _render_disk() -> None:
    """What ap-explanation and ProvSQL write to disk, and which questions write the most."""
    sweep = load_provsql_sweep()
    questions = {q["question_id"]: q for q in load_question_set()}
    names = {p["id"]: p["question"] for p in _PRESETS} | {q: v["question"] for q, v in questions.items()}
    growth: dict[str, int] = {}
    for run in (RUN_NEXT, RUN_NEXT_ADAPTED):
        for q, r in sweep[run].items():
            growth[q] = max(growth.get(q, 0), r["store_growth_mb"])
    memory = load_service_memory_runs()
    spilled = [q for q, r in memory.items() if service_outcome(r) == "disk"]

    st.markdown(
        "Three things fill the database's disk: the annotation of the tables, once; ProvSQL's "
        "circuit store, a little with every query; and temporary files, while a large join runs.")

    st.subheader("1. Annotating the tables: one mapping table, about 0.6× the table")
    st.markdown(
        "ap-explanation adds a `provsql` column to every table it explains, and **one mapping "
        "table**, about half the size of the table, shared by every semiring (it used to build "
        "one per semiring, five in all, 3× the table). It is done once per table.")
    st.dataframe(
        [{"Table": "meteo_elevation, City of Zurich cell", "Rows": "134,280",
          "Table, annotated": "29 MB", "Mapping table": "17 MB", "Annotation took": "seconds"},
         {"Table": "meteo_elevation, the 307 cells of the regions", "Rows": "41.2 million",
          "Table, annotated": "≈ 9 GB", "Mapping table": "≈ 5 GB",
          "Annotation took": "an hour (ap-explanation then reported a failure: its lock had expired)"},
         {"Table": "Each readings table (tmin, tp…), City of Zurich", "Rows": "5,478",
          "Table, annotated": "1 MB", "Mapping table": "< 1 MB", "Annotation took": "under a second"}],
        width="stretch", hide_index=True)
    st.caption(
        "Sizes measured on the databases the measurements ran on (`scripts/apx_service_check.py`, "
        "with ap-explanation master, which still built five mapping tables): one mapping table "
        "is the size of each of those five, so the shared one is not re-measured. The regions "
        "database held 45.6 GB with the five; the regions sizes are scaled from Zurich's.")

    st.subheader("2. ProvSQL's circuit store: grows with every query until cleaned up")
    top = sorted((q for q in growth if growth[q] >= 100), key=lambda q: -growth[q])
    st.markdown(
        "ProvSQL 1.13 writes the provenance circuit of every query to files in the database "
        "directory (`provsql_*.mmap`), and keeps it after the query ends, **even when the query "
        "is stopped or fails**: by design, it only grows. Most questions add nothing or about "
        "40 MB; these add more:")
    st.dataframe(
        [{"Id": q, "Question": names.get(q, ""), "Store growth": format_mb(growth[q])} for q in top],
        width="stretch", hide_index=True)
    st.markdown(
        "The store is also held in memory by ProvSQL's store worker, which is the process the "
        "kernel killed in most of the *Memory* runs. When the disk filled up during these "
        "measurements, that worker died (SIGBUS) and took the database down with it, instead of "
        "failing the query: ProvSQL grows its files with `ftruncate`, which reserves no space.\n\n"
        "`SELECT provsql.circuit_cleanup()` (ProvSQL 1.13) removes the gates no table references "
        "and rewrites the files: it took a 766 MB store back to 4 MB in half a second. Like "
        "`DROP DATABASE`, it needs the database to itself, so it has to be scheduled while "
        "nothing is connected.")
    st.caption(
        "Measured on ProvSQL 1.13 on the demo's data (`scripts/provsql_compare.py`), queries "
        "stopped after 120 s. ProvSQL 1.12 grew it by nothing on the same questions: it refused "
        "them.")

    st.subheader("3. Temporary files: large joins")
    st.markdown(
        "Readings joined to the elevation points of a region produce more rows than fit in "
        "memory, and PostgreSQL writes them to temporary files: up to 10 GB within minutes for "
        "the plain SQL of these questions on the regions data. PostgreSQL sets no limit by "
        "default (`temp_file_limit`); the measurements set 3 GB, which stopped "
        + ", ".join(f"question {q} ({questions[q]['question']})" for q in spilled)
        + ", in a parallel worker.")


tab_summary, tab_summary_next, tab_summary_master, tab_summary_114, tab_memory, tab_disk, tab_runner = st.tabs(
    ["📋 Summary - ProvSQL 1.12", "📋 Summary - ProvSQL 1.13",
     f"📋 Summary - ProvSQL 1.14 - master@{PROVSQL_MASTER_COMMIT}", "📋 Summary - ProvSQL 1.14.0",
     "🧠 Memory", "💽 Disk",
     "🔎 Demo query runner"])

with tab_summary:
    _render_question_summary()

with tab_summary_next:
    _render_question_summary("1.13")

with tab_summary_master:
    _render_question_summary("1.14")

with tab_summary_114:
    _render_question_summary("1.14.0")

with tab_memory:
    _render_memory()

with tab_disk:
    _render_disk()

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
