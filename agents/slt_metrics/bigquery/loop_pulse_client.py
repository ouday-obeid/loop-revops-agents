"""Loop Pulse BigQuery client — gap-flag-first.

Contract:
  - `is_connected()` — cheap: True only if creds are configured AND the
    google-cloud-bigquery package is importable. Safe to call every run.
  - `query(sql, params)` — raises `BigQueryUnavailable` on any terminal
    failure (unreachable, auth, missing package); writes an
    `integration_health` row per attempt so the OO morning brief surfaces
    the break. Retries 3× with jittered backoff.

Design-choice: we don't take a hard dependency on google-cloud-bigquery. The
import is deferred so unit-tests and the gap-flag path both run on stock
Python. Callers that need a real client install the `[bigquery]` extra.

When `BQ_CREDENTIALS_JSON` is a path, we read-through to the file; when it's
a full JSON blob we parse it in-memory. Either works with google-auth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

_INTEGRATION_NAME = "slt_loop_pulse"
_RETRY_BASE_SECONDS = 0.5
_RETRY_MAX_ATTEMPTS = 3
_ERROR_MSG_TRUNCATE = 300


class BigQueryUnavailable(RuntimeError):
    """Raised when a BQ query cannot complete after retries or when the client
    was never properly configured. Callers catch this and fall back to gap-flag
    rendering so a single BQ outage never blocks a revenue model run."""


class LoopPulseClient:
    """Lazy BigQuery client — probe-cheap, fail-loud, retry-with-backoff."""

    def __init__(
        self,
        *,
        creds_json: str | None = None,
        project: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self._creds_json = creds_json or get_config("BQ_CREDENTIALS_JSON")
        self._project = project or get_config("BQ_PROJECT")
        self._client_factory = client_factory
        self._cached_client: Any = None
        self._last_probe_ok: bool | None = None

    # -------------------------------------------------- configuration

    def is_connected(self) -> bool:
        """Return True if creds + package + last probe succeeded.

        Cached for the lifetime of the instance — a long-running daemon that
        wants to re-probe after a known outage should construct a fresh
        client. This matches the Outbounder pattern O is used to.
        """
        if self._last_probe_ok is not None:
            return self._last_probe_ok
        if not self._creds_json or self._creds_json == "REPLACE":
            self._last_probe_ok = False
            return False
        try:
            client = self._resolve_client()
        except Exception as exc:
            log.info("loop_pulse: client init failed — %s", exc)
            self._last_probe_ok = False
            return False
        self._last_probe_ok = client is not None
        return self._last_probe_ok

    # -------------------------------------------------- query

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a parameterized BQ query. Retries 3× before raising BigQueryUnavailable.

        `params` accepts simple scalars — dates/strings/ints. We build the
        BigQuery ScalarQueryParameter list lazily to keep the import out of
        the non-BQ path.
        """
        if not self.is_connected():
            self._record_health("down", "client not configured")
            raise BigQueryUnavailable("Loop Pulse client not configured")

        last_error: Exception | None = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                rows = self._run_query(sql, params or {})
                self._record_health("healthy", None)
                return rows
            except Exception as exc:
                last_error = exc
                log.warning(
                    "loop_pulse query attempt %d/%d failed: %s",
                    attempt, _RETRY_MAX_ATTEMPTS, exc,
                )
                self._record_health("degraded", f"attempt {attempt}: {exc!s:.200s}")
                if attempt >= _RETRY_MAX_ATTEMPTS:
                    break
                time.sleep(_backoff(attempt))

        self._record_health("down", str(last_error)[:_ERROR_MSG_TRUNCATE] if last_error else None)
        raise BigQueryUnavailable(
            f"Loop Pulse query failed after {_RETRY_MAX_ATTEMPTS} attempts: {last_error!s}"
        )

    async def query_async(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """asyncio shim — offloads the sync client call to a worker thread."""
        return await asyncio.to_thread(self.query, sql, params)

    # -------------------------------------------------- internals

    def _resolve_client(self) -> Any:
        """Resolve and cache a bigquery.Client-compatible object.

        Dependency-injected `client_factory` wins — used by tests and the
        unit-econ orchestrator when a caller wants a custom client (e.g.
        pre-provisioned service account scope).
        """
        if self._cached_client is not None:
            return self._cached_client
        if self._client_factory is not None:
            self._cached_client = self._client_factory(
                creds_json=self._creds_json, project=self._project,
            )
            return self._cached_client
        # Real BigQuery path — import lazily so gap-flag callers don't need
        # google-cloud-bigquery installed.
        try:
            from google.cloud import bigquery  # type: ignore
            from google.oauth2 import service_account  # type: ignore
        except ImportError as exc:
            raise BigQueryUnavailable(
                "google-cloud-bigquery not installed; "
                "add the [gcp] extra or inject a client_factory"
            ) from exc
        creds_dict = _load_credentials(self._creds_json)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        self._cached_client = bigquery.Client(
            credentials=credentials,
            project=self._project or creds_dict.get("project_id"),
        )
        return self._cached_client

    def _run_query(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        client = self._resolve_client()
        # Client may be our test double with `.query(sql, params)` directly —
        # accept either contract.
        if hasattr(client, "execute_query"):
            return list(client.execute_query(sql, params))
        # google-cloud-bigquery path.
        job_config = _build_job_config(params)
        job = client.query(sql, job_config=job_config)
        return [dict(row) for row in job.result()]

    def _record_health(self, status: str, error: str | None) -> None:
        engine = get_engine()
        now = datetime.now(timezone.utc)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """INSERT INTO integration_health
                               (integration, status, last_success, last_failure,
                                error_message, checked_at)
                           VALUES (:i, :s, :ls, :lf, :e, :now)"""
                    ),
                    {
                        "i": _INTEGRATION_NAME,
                        "s": status,
                        "ls": now if status == "healthy" else None,
                        "lf": now if status != "healthy" else None,
                        "e": error,
                        "now": now,
                    },
                )
        except Exception:
            log.exception("loop_pulse: integration_health write failed")


# ------------------------------------------------------------------ helpers

def _backoff(attempt: int) -> float:
    return _RETRY_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.2)


def _load_credentials(creds: str) -> dict[str, Any]:
    """Accept either a JSON blob or a filesystem path."""
    stripped = creds.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    path = Path(stripped).expanduser()
    return json.loads(path.read_text(encoding="utf-8"))


def _build_job_config(params: dict[str, Any]) -> Any | None:
    """Convert our simple dict to a google-cloud-bigquery QueryJobConfig.

    Deferred import keeps the gap-flag path free of GCP deps.
    """
    if not params:
        return None
    from google.cloud import bigquery  # type: ignore
    scalars = [
        bigquery.ScalarQueryParameter(name, _bq_type_for(value), value)
        for name, value in params.items()
    ]
    return bigquery.QueryJobConfig(query_parameters=scalars)


def _bq_type_for(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    # Date / datetime / str all land here — BigQuery coerces ISO strings.
    return "STRING"
