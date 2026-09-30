# Changelog

## Unreleased

### New Features

- **Summary tab** — how many questions of the meteo question set ap-explanation supports (548 of 752), and why the 204 others are not, grouped by cause, with the questions and an example of each.
- **Memory analysis tab** — the questions whose provenance can exhaust the database's memory, and why: comparisons on a nested aggregate (memory about 3× per reading of the compared group) and aggregates over many rows (about 3 KB per row of the largest group; readings joined to elevation points multiply them). Each question gets an estimate, and the City of Zurich questions the demo's data can answer a measurement on a 3 GB-capped database. Produced by `scripts/memory_analysis.py`, `scripts/memtest_group_size.sh` and `scripts/memtest_questions.py`.
- **Run cost** — after a live run, the Query runner shows its total time, the database time and peak database memory (the run's PostgreSQL backends, sampled every 0.1 s), and the plain SQL time. Live runs stamp the AP's `startTime`, so ap-explanation computes them afresh instead of returning its cached result.

### Misc

- The Analytical Pattern graph is no longer shown; the runner now lives in the **Query runner** tab.

## v0.1.0 — 2026-09-30

### New Features

- **Weather questions explained with provenance** — six ERA5 questions over the City of Zurich cell (G81, F50, K247, C155, T157, Q295), each shown as its Analytical Pattern graph and SQL, the plain SQL answer, the provenance of every result row with the readings it cites, and the LLM explanation ap-explanation writes from it.
- **Live or recorded runs** — *Run on ap-explanation* posts the AP and polls its task (all semirings, or one); *Show recorded run* replays the response saved when the demo was made, with no service needed. Q295 is recorded only: its provenance exhausts the database's memory.
- **Self-contained stack** — `docker-compose.yml` runs ap-explanation, Redis, and PostgreSQL + ProvSQL seeded with the ERA5 readings (`dependencies/postgres-seed/`).
