"""Service-account upload of the revenue-model workbook to Google Drive.

Behaviour:
* If ``GDRIVE_SERVICE_ACCOUNT_JSON`` or ``GDRIVE_FOLDER_ID`` is missing, the
  uploader logs a warning, skips the call, and returns ``GDriveUploadResult``
  with ``uploaded=False`` and the local path as ``link``.
* The `google-api-python-client` + `google-auth` packages are imported lazily
  inside `_resolve_service()` so unit tests (which use an injected service
  factory) don't require the dependency.
* The returned result always contains a ``link`` field — either the shareable
  Drive URL or a ``file://`` local path fallback.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shared.secrets import get_config

log = logging.getLogger(__name__)


_DEFAULT_MIME_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_SCOPES = ("https://www.googleapis.com/auth/drive.file",)


@dataclass(frozen=True)
class GDriveUploadResult:
    uploaded: bool
    file_id: str | None
    link: str
    warning: str | None = None


def _credential_payload(creds_raw: str) -> dict[str, Any]:
    """Accept either the JSON blob directly or a path to the JSON file."""
    if creds_raw.strip().startswith("{"):
        return json.loads(creds_raw)
    path = Path(creds_raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"GDRIVE_SERVICE_ACCOUNT_JSON path does not exist: {creds_raw}")
    with path.open("r") as f:
        return json.load(f)


class GDriveUploader:
    """Upload workbooks to a specific Drive folder with optional test doubles.

    ``service_factory`` returns an object exposing ``files().create(...).execute()``
    and ``permissions().create(...).execute()``; in production this is a
    ``googleapiclient.discovery.Resource``. Tests pass a tiny stub instead.
    """

    def __init__(
        self,
        *,
        folder_id: str | None = None,
        creds_json: str | None = None,
        service_factory: Callable[[dict[str, Any]], Any] | None = None,
        make_public: bool = True,
    ):
        # Resolve from env lazily so tests can monkeypatch get_config.
        self._folder_id = folder_id if folder_id is not None else get_config("GDRIVE_FOLDER_ID")
        self._creds_json = (
            creds_json if creds_json is not None
            else get_config("GDRIVE_SERVICE_ACCOUNT_JSON")
        )
        self._service_factory = service_factory
        self._make_public = make_public

    # --------------------------------------------------------------- probe

    def is_configured(self) -> bool:
        return bool(self._folder_id) and bool(self._creds_json)

    # --------------------------------------------------------------- upload

    def upload(self, local_path: str | Path, *, filename: str | None = None) -> GDriveUploadResult:
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(f"Workbook not found: {path}")

        if not self.is_configured():
            warning = (
                "GDrive env missing (GDRIVE_FOLDER_ID + GDRIVE_SERVICE_ACCOUNT_JSON) — "
                "skipping upload; keeping local path."
            )
            log.warning(warning)
            return GDriveUploadResult(
                uploaded=False, file_id=None,
                link=f"file://{path.resolve()}", warning=warning,
            )

        try:
            creds = _credential_payload(self._creds_json or "")
            service = self._resolve_service(creds)
            target_name = filename or path.name
            file_id = self._create_file(service, path=path, filename=target_name)
            if self._make_public:
                self._grant_anyone_with_link(service, file_id=file_id)
            link = self._view_link(service, file_id=file_id)
            return GDriveUploadResult(uploaded=True, file_id=file_id, link=link)
        except Exception as e:  # noqa: BLE001 — surface local path + warning, never hard-fail
            warning = f"GDrive upload failed ({type(e).__name__}: {e}); keeping local path"
            log.warning(warning)
            return GDriveUploadResult(
                uploaded=False, file_id=None,
                link=f"file://{path.resolve()}", warning=warning,
            )

    # --------------------------------------------------------------- internals

    def _resolve_service(self, creds_payload: dict[str, Any]):
        if self._service_factory is not None:
            return self._service_factory(creds_payload)
        # Deferred imports — only when an actual upload is attempted.
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore

        credentials = service_account.Credentials.from_service_account_info(
            creds_payload, scopes=list(_SCOPES),
        )
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    def _create_file(self, service: Any, *, path: Path, filename: str) -> str:
        media = self._media_body(path)
        metadata = {"name": filename, "parents": [self._folder_id]}
        created = (
            service.files()
            .create(body=metadata, media_body=media, fields="id")
            .execute()
        )
        file_id = created["id"]
        log.info("GDrive upload created file id=%s name=%s", file_id, filename)
        return file_id

    def _grant_anyone_with_link(self, service: Any, *, file_id: str) -> None:
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            fields="id",
        ).execute()

    def _view_link(self, service: Any, *, file_id: str) -> str:
        # Prefer webViewLink when present, else synthesize the canonical URL.
        try:
            info = service.files().get(fileId=file_id, fields="webViewLink").execute()
            link = info.get("webViewLink")
            if link:
                return link
        except Exception:  # noqa: BLE001
            pass
        return f"https://drive.google.com/file/d/{file_id}/view"

    def _media_body(self, path: Path):
        # Deferred import so tests don't require googleapiclient installed.
        from googleapiclient.http import MediaFileUpload  # type: ignore
        return MediaFileUpload(
            str(path), mimetype=_DEFAULT_MIME_XLSX, resumable=False,
        )


# --------------------------------------------------------------- convenience

def upload_workbook(
    local_path: str | Path,
    *,
    filename: str | None = None,
    folder_id: str | None = None,
    creds_json: str | None = None,
    service_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> GDriveUploadResult:
    uploader = GDriveUploader(
        folder_id=folder_id,
        creds_json=creds_json,
        service_factory=service_factory,
    )
    return uploader.upload(local_path, filename=filename)
