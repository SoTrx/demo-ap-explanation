import asyncio
import json
import logging
import threading
import time
from os import getenv
from typing import Self

import httpx
import psycopg
from kiota_abstractions.authentication.anonymous_authentication_provider import (
    AnonymousAuthenticationProvider,
)
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_http.httpx_request_adapter import HttpxRequestAdapter
from kiota_serialization_json.json_serialization_writer import JsonSerializationWriter
from psycopg import sql

from generated.ap_explanation.ap_explanation_client import ApExplanationClient
from generated.ap_explanation.api.v1.aps.explanation.explanation_request_builder import (
    ExplanationRequestBuilder,
)
from generated.ap_explanation.models.provenance_analytical_pattern import (
    ProvenanceAnalyticalPattern,
)
from utils import database_name, extract_sql

AP_EXPLANATION_SERVICE_URL = getenv(
    "AP_EXPLANATION_SERVICE_URL", "http://ap-explanation:5000")
# libpq connection string of the database the APs query (without ``dbname``, it is
# taken from the AP). Unset: the demo shows no plain SQL answer on live runs.
PLAIN_DSN = getenv("PLAIN_DSN")

# Task states in which ap-explanation's Celery task has not settled yet.
_UNSETTLED = ("pending", "started", "retry")
_POLL_INTERVAL_SECONDS = 1
# A run is mostly the LLM call (LLM_TIMEOUT defaults to 300 s on the service).
POLL_TIMEOUT_SECONDS = 600

_log = logging.getLogger(__name__)


def _create_adapter(base_url: str) -> HttpxRequestAdapter:
    adapter = HttpxRequestAdapter(AnonymousAuthenticationProvider())
    adapter.base_url = base_url
    return adapter


def _client() -> ApExplanationClient:
    return ApExplanationClient(_create_adapter(AP_EXPLANATION_SERVICE_URL))


def _kiota_model_to_dict(model) -> dict:
    """Re-serialize a Kiota response model to the plain JSON dict it came from.

    Going back through the JSON writer collapses the ``anyOf`` composed-type
    wrappers Kiota generates (``TaskStatusResponse_error`` &c.) and keeps the
    untyped ``result`` field, which Kiota leaves in ``additional_data``.
    """
    if model is None:
        return {}
    writer = JsonSerializationWriter()
    writer.write_object_value(None, model)
    return json.loads(writer.get_serialized_content() or "{}")


def service_healthy() -> bool:
    try:
        return httpx.get(
            f"{AP_EXPLANATION_SERVICE_URL.rstrip('/')}/api/v1/health", timeout=3
        ).is_success
    except httpx.HTTPError:
        return False


async def submit_explanation(ap_data: dict, semiring: str | None = None) -> str:
    """``POST /api/v1/aps/explanation[/{semiring}]?probability=false``; returns the
    task id. Without ``semiring``, every semiring is computed (only ``formula``
    on aggregate queries)."""
    body = ProvenanceAnalyticalPattern(
        {k: v for k, v in ap_data.items() if k != "$schema"})
    config = RequestConfiguration(
        query_parameters=ExplanationRequestBuilder.ExplanationRequestBuilderPostQueryParameters(
            probability=False))
    explanation = _client().api.v1.aps.explanation
    try:
        if semiring:
            response = await explanation.by_semiring_name_id(semiring).post(body, config)
        else:
            response = await explanation.post(body, config)
    except Exception:
        _log.exception("ap-explanation POST /explanation call failed")
        raise
    if not response or not response.task_id:
        raise RuntimeError("ap-explanation returned no task_id")
    return response.task_id


async def poll_explanation(task_id: str) -> dict:
    """Poll ``GET /api/v1/aps/explanation/{task_id}`` until the task settles.
    Returns ``{task_id, status, result, error}``."""
    # The spec's ``{semiring_name}`` POST and ``{task_id}`` GET share one path
    # segment, which Kiota merges into ``by_semiring_name_id``: pin the URL instead.
    base = f"{AP_EXPLANATION_SERVICE_URL.rstrip('/')}/api/v1/aps/explanation"
    item = _client().api.v1.aps.explanation.by_semiring_name_id(task_id).with_url(
        f"{base}/{task_id}")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        status = _kiota_model_to_dict(await item.get())
        if status.get("status") not in _UNSETTLED:
            return status
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"ap-explanation task {task_id} did not finish in {POLL_TIMEOUT_SECONDS} s")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def explain(ap_data: dict, semiring: str | None = None) -> dict:
    """Submit an AP and wait for its explanation. Adds the run time in ``seconds``."""
    t0 = time.monotonic()
    task_id = await submit_explanation(ap_data, semiring)
    status = await poll_explanation(task_id)
    return {**status, "seconds": round(time.monotonic() - t0, 1)}


def run_plain(ap_data: dict) -> dict | None:
    """The AP's SQL run without provenance: ``{columns, rows, seconds}``, or
    ``None`` when ``PLAIN_DSN`` is unset."""
    if not PLAIN_DSN:
        return None
    query, schema = extract_sql(ap_data), database_name(ap_data)
    t0 = time.monotonic()
    with psycopg.connect(PLAIN_DSN, dbname=schema, connect_timeout=5) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        # Tables stay annotated between runs; ProvSQL is off unless a session
        # turns it on, but say so explicitly.
        conn.execute("SET provsql.active = 0")
        cur = conn.execute(query)
        return {"columns": [c.name for c in cur.description], "rows": cur.fetchall(),
                "seconds": round(time.monotonic() - t0, 2)}


# Private memory (RssAnon) and state of every client backend on a database, except the
# sampling one. /proc is read through pg_read_file, which needs a superuser (the demo's
# provdemo is one); missing_ok covers a backend that exits between the two reads.
_SAMPLE_QUERY = r"""
SELECT a.pid, a.state,
       substring(pg_read_file('/proc/' || a.pid || '/status', true)
                 FROM 'RssAnon:\s+(\d+) kB')::bigint AS rss_anon_kb
FROM pg_stat_activity AS a
WHERE a.datname = %s AND a.backend_type = 'client backend' AND a.pid <> pg_backend_pid()
"""


class DbMonitor:
    """Samples, in a thread, the database backends serving a live run: their summed
    private memory and whether one of them is running a statement.

    ap-explanation opens a fresh connection pool per task, so the backends that appear
    after entering the context are the run's; those already there are ignored.
    """

    def __init__(self, dbname: str, interval: float = 0.1):
        self._dbname = dbname
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: psycopg.Connection | None = None
        self._baseline: set[int] = set()
        self._peak_kb = 0
        self._busy_seconds = 0.0
        self._error: str | None = None

    def __enter__(self) -> Self:
        if not PLAIN_DSN:
            self._error = "PLAIN_DSN is not set"
            return self
        try:
            self._conn = psycopg.connect(
                PLAIN_DSN, dbname=self._dbname, connect_timeout=5, autocommit=True)
            self._baseline = {pid for pid, _, _ in self._sample()}
        except psycopg.Error as exc:
            self._error = str(exc).strip()
            return self
        self._thread = threading.Thread(target=self._run, daemon=True, name="db-monitor")
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        if self._conn:
            self._conn.close()

    def _sample(self) -> list[tuple]:
        # Autocommit: each sample is its own transaction, so pg_stat_activity is fresh.
        return self._conn.execute(_SAMPLE_QUERY, (self._dbname,)).fetchall()

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self._interval):
            try:
                rows = [r for r in self._sample() if r[0] not in self._baseline]
            except psycopg.Error as exc:
                self._error = str(exc).strip()
                return
            now = time.monotonic()
            self._peak_kb = max(self._peak_kb, sum(r[2] or 0 for r in rows))
            if any(r[1] == "active" for r in rows):
                self._busy_seconds += now - last
            last = now

    def report(self) -> dict:
        """``{peak_mb, busy_seconds}``, or ``{error}`` when sampling was impossible."""
        if self._error:
            return {"error": self._error}
        return {"peak_mb": round(self._peak_kb / 1024, 1),
                "busy_seconds": round(self._busy_seconds, 1)}
