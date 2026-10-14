# Changelog

## Unreleased

### New Features

- **Summary - ProvSQL 1.13 tab** — the Summary table (renamed *Summary - ProvSQL 1.12*) on ProvSQL 1.13 (master at `45320ea7`), measured through ap-explanation itself: as is (v1.2.0), 440 questions run, 64 may exhaust memory, 248 are not supported; patched for ProvSQL 1.13 (branch `feat/provsql-1.13-possible-worlds`), 564, 48 and 140. Each question says what blocks it (ProvSQL, ap-explanation, a ProvSQL bug, its own SQL, the demo's data) and what would remove it. The questions that may exhaust memory were run on the demo's data copied to their region, with `provsql.max_memory = '1GB'`.
- **ProvSQL 1.13 tab: through ap-explanation itself** — every template's City of Zurich question sent to the service on ProvSQL 1.12 and 1.13, as is and patched: ProvSQL 1.13 breaks 23 of the supported templates that work on ProvSQL 1.12, and the patch brings back all of them; `provsql.max_memory` does not keep the database up on the questions that may exhaust memory. Produced by `scripts/apx_service_check.py`.
- **ProvSQL 1.13 tab** — what the next ProvSQL (not released; its master branch at `45320ea7`, 14 October 2026) changes for the question set, measured against ProvSQL 1.12 on the City of Zurich question of each unsupported template and on the six demo questions: which reasons for being unsupported it lifts, what ap-explanation has to change in the queries it sends (`LIMIT plain(k)`, `present(provenance())`, no `HAVING` refusal) and what breaks otherwise (G81, C155, T157), the run-time problems it solves (cancellation, a refusal instead of running out of memory on a comparison on an aggregate) and those it leaves open, among them the reach of `provsql.max_memory`, rerun after its fix (provsql#104). The Summary tab gains a *With ProvSQL 1.13* column. Produced by `scripts/provsql_compare.py` and `scripts/memtest_group_size.sh`.
- **Summary tab** — all 752 questions in a spreadsheet-like table, coloured green (runs), yellow (may exhaust memory, above the 3 GB limit) or red (not supported, or failing at run time), with counts and a status filter.
- **Memory analysis: measurement setup and semirings** — the 3 GB limit (`--memory=3g --memory-swap=3g`), the databases measured and their row counts, and which semirings each shape concerns: shape 1 every semiring but `boolexpr` (and not the probability), shape 2 `formula` only. `scripts/memtest_group_size.sh` measures every semiring, `scripts/memtest_questions.py` takes `--semiring=`.
- **Theoretically supported questions tab** (the former Summary tab) — how many questions of the meteo question set ap-explanation supports (548 of 752), and why the 204 others are not, grouped by cause, with the questions and an example of each.
- **Memory analysis tab** — the questions whose provenance can exhaust the database's memory, and why: comparisons on a nested aggregate (memory about 3× per reading of the compared group) and aggregates over many rows (about 3 KB per row of the largest group; readings joined to elevation points multiply them). Each question gets an estimate, and the City of Zurich questions the demo's data can answer a measurement on a 3 GB-capped database. Produced by `scripts/memory_analysis.py`, `scripts/memtest_group_size.sh` and `scripts/memtest_questions.py`.
- **Run cost** — after a live run, the Query runner shows its total time, the database time and peak database memory (the run's PostgreSQL backends, sampled every 0.1 s), and the plain SQL time. Live runs stamp the AP's `startTime`, so ap-explanation computes them afresh instead of returning its cached result.

### Misc

- `assets/meteo_queries.csv` holds the SQL of every question (ap-explanation's `fixtures/meteo_queries.csv`), not only of the unsupported ones.
- The *Summary - ProvSQL 1.12* tab counts as not supported the 9 supported templates that return another value than plain SQL through ap-explanation on ProvSQL 1.12 (arithmetic on aggregates): 462 questions run instead of 498.
- The scripts remove a container's anonymous volume with it (`docker rm -v`).
- `scripts/provsql_compare.py sweep` takes `--max-memory=`, to set `provsql.max_memory` for every question.
- `scripts/memtest_group_size.sh` records a query ProvSQL refuses as `refused`, and no longer needs `bc`.
- The Analytical Pattern graph is no longer shown; the runner now lives in the **Query runner** tab.

## v0.1.0 — 2026-09-30

### New Features

- **Weather questions explained with provenance** — six ERA5 questions over the City of Zurich cell (G81, F50, K247, C155, T157, Q295), each shown as its Analytical Pattern graph and SQL, the plain SQL answer, the provenance of every result row with the readings it cites, and the LLM explanation ap-explanation writes from it.
- **Live or recorded runs** — *Run on ap-explanation* posts the AP and polls its task (all semirings, or one); *Show recorded run* replays the response saved when the demo was made, with no service needed. Q295 is recorded only: its provenance exhausts the database's memory.
- **Self-contained stack** — `docker-compose.yml` runs ap-explanation, Redis, and PostgreSQL + ProvSQL seeded with the ERA5 readings (`dependencies/postgres-seed/`).
