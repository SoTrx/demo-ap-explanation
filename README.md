# Provenance Explorer: Zurich weather

A Streamlit app about [**ap-explanation**](https://github.com/datagems-eosc/ap-explanation)
and the meteo question set, in five tabs.

**📋 Summary - ProvSQL 1.12** — all 752 questions in one table, each row coloured: 🟢 runs without issue,
🟡 may exhaust memory (estimated above the 3 GB limit used for the measurements), 🔴 not
supported (rejected by ap-explanation, failing at run time, or not returning the SQL answer:
9 supported templates do arithmetic on aggregates that ProvSQL 1.12 gets wrong). 462 run, 46
may exhaust memory, 244 are not supported. A column gives each question's status on ProvSQL 1.13.

**📋 Summary - ProvSQL 1.13 (master at `45320ea7`)** — the same table on ProvSQL 1.13, measured
through ap-explanation itself, twice:
- **as is** (v1.2.0, as deployed): 440 run, 64 may exhaust memory, 248 are not supported. ProvSQL
  1.13 breaks 23 of the supported templates that work on ProvSQL 1.12 (wrong rows, a database
  killed, not finished);
- **patched** (ap-explanation branch `feat/provsql-1.13-possible-worlds`: `LIMIT plain(k)`, rows
  kept where `present(provenance())`, `HAVING` lifted, only on ProvSQL 1.13): 564 run, 48 may
  exhaust memory, 140 are not supported, and every supported template that works on ProvSQL 1.12
  works.

Each question also says what blocks it (ProvSQL, ap-explanation, a ProvSQL bug, its own SQL, the
demo's data) and what would remove it. Each template was run on its City of Zurich question; the
82 questions that may exhaust memory were run themselves, on the demo's data copied to every
cell of their region, with `provsql.max_memory = '1GB'`: it stopped one of them, the database
was killed on 16 (ProvSQL's circuit-store worker and parallel workers grow outside the budget),
and `provsql.max_worlds` refuses the comparisons on aggregates cleanly.

**📊 Theoretically supported questions** — how many of the set's 752 questions ap-explanation supports (548) and why the
other 204 are not, grouped by cause (scalar subquery reading a CTE, `HAVING`, `ORDER BY` on an
aggregate, aggregates ProvSQL cannot explain, window functions, subqueries in conditions, and a
ProvSQL bug on `COUNT(DISTINCT …)`), with the questions and an example for each. The data is
ap-explanation's `fixtures/meteo_queries.csv`, produced by its
`scripts/classify_meteo_queries.py` (`assets/meteo_queries.csv` here, with the SQL of every
question).

**🧠 Memory analysis** — the questions whose provenance can exhaust the database's memory,
and why. All measurements ran on a PostgreSQL + ProvSQL server limited to **3 GB of RAM and no
swap** (`docker run --memory=3g --memory-swap=3g`); the tab lists its databases and their row
counts. Two query shapes make ProvSQL's evaluation outgrow memory, and ProvSQL 1.12
ignores cancellation while evaluating, so only the OOM killer or a restart ends the run:

1. **A comparison on an aggregate computed in a CTE or subquery** (Q295's `WHERE
   total_precipitation > 0.001` on monthly sums, or a `HAVING` rewritten as a nested
   `SELECT`): ProvSQL expands it over the combinations of the group's readings, about 3× the
   memory per reading (0.9 GB at 20 readings, over 3 GB at 22).
2. **An aggregate over many rows**: the formula lists every row the aggregate reads, about
   3 KB each, and memory follows the largest group. That adds up over the 134,280 elevation
   points of a cell (400 MB), over large regions, and above all when readings are joined to
   the elevation table, each reading paired with every point of its cell (1.85 GB for one
   month above 500 m).

Shape 1 concerns every semiring but `boolexpr` (`formula` and `why` from 22 readings, `how`
and `which` from 24–26; the probability is unaffected), shape 2 `formula` only, the one
semiring ap-explanation explains aggregates with.

The tab lists every question with either shape, its estimated memory, and, for the City of
Zurich questions the demo's data can answer, the peak measured on a 3 GB-capped database.
It reads files produced by three scripts:

| Script | Output | What it does |
|--------|--------|--------------|
| `scripts/memory_analysis.py` | `assets/memory_risk.csv` | Finds both shapes in the SQL of every question (sqlglot) and estimates their memory |
| `scripts/memtest_group_size.sh` | `assets/memory_group_size.csv` | Measures shape 1 against the size of the compared group |
| `scripts/memtest_questions.py` | `assets/memory_sweep.jsonl` | Runs questions through ap-explanation and records their peak memory |

The two measurement scripts need a throwaway, memory-capped database; their docstrings give
the commands. Never run them against a shared database.

<a id="provsql-113"></a>
**🆕 ProvSQL 1.13** — what the next ProvSQL changes for the question set. The demo runs
ProvSQL 1.12; 1.13 is not released, so its development branch (master at `45320ea7`,
14 October 2026) was built on the demo's database image and given the same queries: the City
of Zurich question of each of the 51 unsupported templates (the 204 unsupported questions are
these templates asked of four regions) and the six demo questions, with the `formula`
semiring, each in a database of its own capped at 3 GB.

- **More questions are explained, once ap-explanation changes the queries it sends.** 25 of
  the 51 templates then run and return the plain SQL answer, against 2 on ProvSQL 1.12, and 5
  more run with an empty answer on the demo's data. `ORDER BY` on an aggregate, `HAVING` and
  window functions work; `PERCENTILE_CONT`, `REGR_SLOPE` and `CORR` are still not explained;
  the *lowest / highest point* templates run but do not return the SQL answer.
- **Sent as they are today, the queries break.** ProvSQL 1.13 reads `ORDER BY … LIMIT k` in
  every possible world, so G81 runs out of memory and C155 and T157 do not finish. Every
  `LIMIT k` has to become `LIMIT plain(k)`, the rows have to be filtered by
  `present(provenance())`, and ap-explanation has to stop refusing `HAVING`. Refusals also
  change form (SQLSTATE `0A000`, with a `provsql-reason` on the `DETAIL` line).
- **A comparison on an aggregate is refused instead of exhausting memory**, past
  `provsql.max_worlds` possible worlds of the group (2²⁰ by default, 20 readings). Under that
  it costs what it did, so Q295 gets an error instead of an answer. Cancellation now works.
  Aggregates over many rows cost the same.
- **`provsql.max_memory` stops the evaluation, not the database's growth.** It did not stop the
  two evaluations it was first tried on (reported as
  [provsql#104](https://github.com/PierreSenellart/provsql/issues/104), fixed on master on
  13 October 2026): G81 now stops at 1 GB instead of being killed at 3 GB, and a 1 GB budget kills
  no backend over the 57 questions. The numbers above are the same on both builds. Still open:
  the budget is not checked while the possible worlds of a comparison are listed (the months of
  2019 with `max_worlds = 0` are killed at 3 GB), question 412 runs to the end at 1.7 GB under it,
  and ProvSQL's circuit store, shared by the whole database, grew by 4 GB on one question. On
  the questions that may exhaust memory, run on their region's data, the database was killed on
  16 of 46 under a 1 GB budget: the circuit store's worker and parallel workers grow outside it.

| Script | Output | What it does |
|--------|--------|--------------|
| `scripts/provsql_compare.py sweep` | `assets/provsql_113_sweep.jsonl` | Runs the questions on a ProvSQL version, as sent today or adapted, and records outcome, time, memory and whether the answer is the plain SQL one |
| `scripts/provsql_compare.py checks` | `assets/provsql_113_checks.jsonl` | Runs the run-time problems one by one: comparison on an aggregate, top-k, large aggregates, cancellation, memory limit |
| `scripts/provsql_compare.py sweep --max-memory=1GB` | `assets/provsql_113_sweep_maxmem1g.jsonl` | The same questions with `provsql.max_memory = '1GB'` |
| `scripts/memtest_group_size.sh` | `assets/memory_group_size_113.csv` | The measurement of shape 1, on ProvSQL 1.13 |
| `scripts/provsql_compare.py sweep --adapted --supported=assets/meteo_queries.csv` | `assets/provsql_113_supported_adapted.jsonl` | The supported City of Zurich questions, adapted |
| `scripts/apx_service_check.py run … zurich` | `assets/apx_service_{113,112}.jsonl`, `assets/apx_service_{113,112}_patched.jsonl` | The City of Zurich question of every template sent to ap-explanation itself (as is, and patched with `APX_IMAGE`), which annotates the tables and asks for every semiring |
| `scripts/apx_service_check.py run … --max-memory=1GB --reuse` | `assets/apx_service_113_memory.jsonl`, `assets/apx_service_113_memory_patched.jsonl` | The questions that may exhaust memory, on the demo's data copied to their region's cells (`prepare … --regions`) |

`scripts/provsql_compare.py` needs a checkout of ap-explanation at v1.2.0 (for its SQL rewriter)
and one database image per ProvSQL version, annotated by `scripts/provsql_compare_setup.sql`;
`scripts/apx_service_check.py` needs the ap-explanation image and snapshots the service itself
annotated. Their docstrings give the commands. The `--regions` snapshot holds 41 million
elevation points: a container started from it copies its 44 GB on first write, hence `--reuse`. The tables the demo's seed lacks (`meteo_tmean`, `meteo_sdmax`,
`meteo_ssrd`, land cover) are stand-ins derived from the readings at hand
(`scripts/meteo_standins.sql`).

**🔎 Query runner** — runs a question through ap-explanation and shows:

| Section | What it shows | Source |
|---------|---------------|--------|
| **SQL** | The query of the AP's `Provenance_SQL_Operator` | `assets/aps/<ID>.json` |
| **Run cost** | Total time, database time, peak database memory, plain SQL time | see [Run cost](#run-cost) |
| **1. Plain SQL answer** | What SQL alone returns | the query run on the database, without ProvSQL |
| **2. What provenance adds** | Per result row, the ProvSQL formula and the readings it cites (date, place, value in °C / mm / m/s) | `ap-explanation` `POST /api/v1/aps/explanation[/{semiring}]` |
| **3. LLM explanation** | The explanation ap-explanation writes from the query, its answer and the provenance | same call |

Each question can be **run live** on ap-explanation (about 15–40 s, mostly the LLM call) or
**replayed** from the response recorded when the demo was made (`assets/recorded/`), which needs
no service at all.

## The questions

Six questions from the meteo question set, over one ERA5 cell (City of Zurich, 47.4 / 8.5,
2005–2019). They come from ap-explanation's `fixtures/meteo_examples/` (see its `NOTES.md`).

| AP | Question | Tables | What the provenance adds |
|----|----------|--------|--------------------------|
| G81 | 10 lowest temperatures, July 2015 | `meteo_tmin` | the date of each temperature |
| F50 | Number of readings above 30 °C in 2015 | `meteo_tmax` | the 13 hot days behind the count |
| K247 | Frost days in each January, 2005–2015 | `meteo_tmin` | the dated frost days behind each yearly count |
| C155 | Location of the highest wind speed, 2018–2019 | `meteo_windspeedmax`, `meteo_elevation` | the day of the max (storm Burglind), and that the elevation join is useless |
| T157 | Highest precipitation at the highest point, ISO week 46 of 2011 | `meteo_tp`, `meteo_elevation` | the 7 dry days of the week, each paired with the highest point |
| Q295 | Last month of 2019 with more than 1 mm of precipitation | `meteo_tp` | nothing: **recorded failure only**, see [Known limitations](#known-limitations) |

---

## Run cost

After a live run, the Query runner shows:

- **Total time**: from submitting the AP to the task settling, LLM call included.
- **Database time** and **peak database memory**: a thread samples, every 0.1 s,
  `pg_stat_activity` and each of the run's PostgreSQL backends' `/proc/<pid>/status`
  (through `pg_read_file`, which needs a superuser: the demo's `provdemo` is one). Database
  time adds up the samples where one of the run's backends is running a statement; memory is
  the highest sum of their private memory (`RssAnon`, shared buffers excluded).
  ap-explanation opens a fresh connection pool per task, so the backends that appear during
  the run are the run's.
- **Plain SQL time**: the same query without provenance.

ap-explanation caches results for an hour under a key made of the whole AP. A cache hit
skips all database work, so each live run stamps the AP's `startTime` with the time of the
run, and computes afresh. Recorded runs only have their total and plain SQL times.

---

## Architecture

```mermaid
graph TD
    User(["User"])

    subgraph devcontainer ["Dev Container"]
        Streamlit["Streamlit Frontend<br>main.py · port 8501"]
    end

    subgraph base ["docker-compose.yml"]
        APExpl["AP Explanation<br>ap-explanation · 5000"]
        Redis[("Redis<br>Celery broker + lock")]
        PG[("PostgreSQL + ProvSQL<br>meteo-postgres · DB meteo")]
    end

    LLM["LLM<br>(external — LLM_API_BASE)"]

    User --> Streamlit
    Streamlit -->|"POST /api/v1/aps/explanation · poll task"| APExpl
    Streamlit -.->|"plain SQL (PLAIN_DSN)"| PG
    APExpl --> Redis
    APExpl -->|"annotate tables · query under ProvSQL"| PG
    APExpl -.->|"explain the provenance"| LLM
```

**Flow of a live run:**

1. The frontend POSTs the AP to ap-explanation and gets a `task_id`. Picking a semiring
   posts to `/{semiring}`; the default computes them all (aggregate queries only get
   `formula`).
2. ap-explanation's Celery task annotates the AP's tables with provenance, runs the query
   under ProvSQL, resolves the rows each formula cites, then asks the LLM to explain it.
3. The frontend polls `GET /api/v1/aps/explanation/{task_id}` until the task settles, and
   runs the same query plainly on `PLAIN_DSN` for comparison.

The database (`meteo-postgres`) is seeded on first start from `dependencies/postgres-seed/`:
`01_meteo.sql` creates schema `meteo` with the column types of the dev server's
`ds_era5_land`, and loads the CSVs in `data/` (fetched from the Open-Meteo archive, model
`era5_seamless`, by ap-explanation's `scripts/meteo_examples/load_era5.py`).

The HTTP client under `generated/` is auto-generated with
[Kiota](https://github.com/microsoft/kiota) from ap-explanation's OpenAPI spec.

---

## Running the demo

### 1. Authenticate with GitHub Container Registry

Sidecar images are on GHCR. Create a [GitHub PAT](https://github.com/settings/tokens)
with the `read:packages` scope, then:

```sh
echo YOUR_GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### 2. Pick the ap-explanation image

The meteo questions need the sql_rewriter changes released **after ap-explanation v1.1.1**
(aggregate provenance, `col_<i>` naming). `docker-compose.yml` pins
`ghcr.io/datagems-eosc/datagems-eosc/ap-explanation:v1.2.0`. Until that tag is published,
build an image from an ap-explanation checkout and point at it in `.env`:

```sh
docker build --target prod -t ap-explanation:local ../../ap-explanation
cp .env.example .env
# then in .env:
# AP_EXPLANATION_IMAGE=ap-explanation
# AP_EXPLANATION_TAG=local
```

### 3. Configure the LLM (optional)

Set `LLM_API_BASE`, `LLM_API_KEY` and `LLM_API_MODEL` in `.env` (an OpenAI-compatible
endpoint). Without them, ap-explanation still computes the provenance, but writes no
explanation.

### 4. Open in the dev container

Open the repo in VS Code → **Reopen in Container**. This starts the sidecars (PostgreSQL +
ProvSQL, Redis, ap-explanation) via `docker-compose.yml`.

### 5. Run the app

```sh
streamlit run main.py
```

With no sidecars at all, the **Show recorded run** button still works; **Run on
ap-explanation** is disabled until `AP_EXPLANATION_SERVICE_URL` answers on `/api/v1/health`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `AP_EXPLANATION_SERVICE_URL` | `http://ap-explanation:5000` | ap-explanation base URL |
| `PLAIN_DSN` | unset | libpq DSN of the database, for the plain SQL answer on live runs |

### Deploying standalone (e.g. Portainer)

Outside the dev container, `docker compose up` reads the same files from a plain
`git clone`. Two gotchas that don't show up in the dev container:

- **Base path.** If a reverse proxy serves the app under a path prefix (the bundled
  Traefik labels use `/demo-explanation`), set `APP_BASE_URL_PATH` (e.g. in Portainer's
  stack environment variables) to that same prefix, or the UI loads a blank page.
- **Seed data.** `meteo-postgres` is built from `dependencies/postgres-seed/Dockerfile`, which
  copies the ERA5 seed SQL and CSVs into the image. They are not bind-mounted because a
  stack deployed from git by Portainer does not see the clone's relative paths on the Docker
  host: the mount came up empty, Postgres logged `ignoring /docker-entrypoint-initdb.d/*`, and
  every query failed with `relation "meteo_tmin" does not exist`. The data is loaded on the
  container's first start only; it has no named volume, so recreating the container re-seeds
  it. To fix a stack stuck on an empty database, redeploy it with the image rebuilt
  (**Update the stack**, with *Re-pull image and redeploy* on). Check with
  `docker compose exec meteo-postgres psql -U provdemo -d meteo -c '\dt meteo.*'`: five
  tables, `meteo_tmin`/`tmax`/`tp`/`windspeedmax` at 5,478 rows each and `meteo_elevation`
  at 134,280.

---

## Known limitations

- **Q295 can take the database down.** Its filter compares a `SUM` computed in a CTE, which
  ProvSQL evaluates as a comparison over an aggregate. On real data it used up 54 GB of memory
  in about 6 minutes, with every semiring and with `formula` alone, and ProvSQL 1.12 ignores
  `statement_timeout` and `pg_terminate_backend` while evaluating it. The demo only replays its
  recorded failure. The same holds for any comparison on an aggregate (HAVING-like filters).
  ProvSQL 1.13 refuses such a comparison instead, see [ProvSQL 1.13](#provsql-113).
- **ProvSQL 1.13 is not a drop-in upgrade.** The demo stays on ProvSQL 1.12: with the queries
  ap-explanation sends today, 1.13 breaks G81, C155 and T157, see [ProvSQL 1.13](#provsql-113).
- **One AP at a time per database.** ap-explanation holds a Redis lock per database while a task
  runs, so concurrent runs queue.
- **Tables stay annotated between runs.** ap-explanation leaves the provenance columns in place.
  They are invisible to plain SQL, since `provsql.active` is off unless a session turns it on.
- **Later runs are cheaper than the first one.** Tables stay annotated, so only the first
  live run of a table pays for annotating it.
- **The recorded explanations are frozen.** The takeaway shown above the provenance was written
  for the recorded run, so it is only shown there.

---

## Regenerating the API client

```sh
make clients
```

The client is generated from the vendored spec `openapi/ap_explanation.json`, so it doesn't
depend on which image is running. Refresh the spec from a running service with:

```sh
curl -s http://ap-explanation:5000/openapi.json > openapi/ap_explanation.json
```
