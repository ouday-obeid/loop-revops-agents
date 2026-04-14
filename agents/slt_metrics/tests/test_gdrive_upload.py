"""GDrive uploader — env-gap fallback, service wiring, failure handling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.slt_metrics.gdrive import uploader as gd


# ---------------------------------------------------------------- fakes

class _FakeDriveService:
    """Minimal fake matching the `googleapiclient.discovery.Resource` surface."""

    def __init__(self, *, raise_on_create: bool = False):
        self.created: list[dict] = []
        self.permissions_calls: list[dict] = []
        self._raise_on_create = raise_on_create
        self._next_id = 0

    def files(self):
        outer = self

        class _Files:
            def create(self_files, *, body, media_body, fields):
                if outer._raise_on_create:
                    raise RuntimeError("simulated drive error")
                outer._next_id += 1
                outer.created.append({
                    "body": body,
                    "media_body": media_body,
                    "fields": fields,
                })

                class _Exec:
                    def execute(_):
                        return {"id": f"fake-id-{outer._next_id}"}
                return _Exec()

            def get(self_files, *, fileId, fields):
                class _Exec:
                    def execute(_):
                        return {"webViewLink": f"https://drive.google.com/file/d/{fileId}/view?x=1"}
                return _Exec()

        return _Files()

    def permissions(self):
        outer = self

        class _Perms:
            def create(self_p, *, fileId, body, fields):
                outer.permissions_calls.append({"fileId": fileId, "body": body})

                class _Exec:
                    def execute(_):
                        return {"id": "perm-1"}
                return _Exec()

        return _Perms()


@pytest.fixture
def workbook(tmp_path) -> Path:
    path = tmp_path / "workbook.xlsx"
    path.write_bytes(b"PK\x03\x04 stub")
    return path


# ---------------------------------------------------------------- is_configured

def test_is_configured_false_without_env(monkeypatch):
    monkeypatch.delenv("GDRIVE_FOLDER_ID", raising=False)
    monkeypatch.delenv("GDRIVE_SERVICE_ACCOUNT_JSON", raising=False)
    uploader = gd.GDriveUploader()
    assert uploader.is_configured() is False


def test_is_configured_true_when_both_present():
    uploader = gd.GDriveUploader(
        folder_id="folder-xyz", creds_json='{"x": 1}', service_factory=lambda _: None,
    )
    assert uploader.is_configured() is True


# ---------------------------------------------------------------- gap-flag / skip

def test_upload_skipped_when_env_missing(monkeypatch, workbook):
    monkeypatch.delenv("GDRIVE_FOLDER_ID", raising=False)
    monkeypatch.delenv("GDRIVE_SERVICE_ACCOUNT_JSON", raising=False)
    result = gd.upload_workbook(workbook)
    assert result.uploaded is False
    assert result.file_id is None
    assert result.link.startswith("file://")
    assert "skipping upload" in (result.warning or "")


def test_upload_raises_when_workbook_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        gd.upload_workbook(tmp_path / "nope.xlsx")


# ---------------------------------------------------------------- happy path

def test_upload_happy_path_returns_drive_link(monkeypatch, workbook):
    fake = _FakeDriveService()
    # Bypass `MediaFileUpload` by patching `_media_body` to a sentinel string.
    monkeypatch.setattr(
        gd.GDriveUploader, "_media_body",
        lambda self, path: f"<media:{path.name}>",
    )
    result = gd.upload_workbook(
        workbook,
        folder_id="folder-xyz",
        creds_json='{"project_id": "x", "type": "service_account"}',
        service_factory=lambda creds: fake,
    )
    assert result.uploaded is True
    assert result.file_id == "fake-id-1"
    assert "drive.google.com" in result.link
    # Metadata wired correctly.
    assert fake.created[0]["body"]["parents"] == ["folder-xyz"]
    assert fake.created[0]["body"]["name"] == "workbook.xlsx"
    # Public-link permission granted.
    assert fake.permissions_calls
    assert fake.permissions_calls[0]["body"] == {"role": "reader", "type": "anyone"}


def test_upload_honors_override_filename(monkeypatch, workbook):
    fake = _FakeDriveService()
    monkeypatch.setattr(gd.GDriveUploader, "_media_body", lambda self, path: None)
    result = gd.upload_workbook(
        workbook, filename="Loop_Revenue_Model_2026-04-13.xlsx",
        folder_id="folder-xyz",
        creds_json='{"x": 1}',
        service_factory=lambda creds: fake,
    )
    assert result.uploaded is True
    assert fake.created[0]["body"]["name"] == "Loop_Revenue_Model_2026-04-13.xlsx"


def test_upload_skips_public_permission_when_disabled(monkeypatch, workbook):
    fake = _FakeDriveService()
    monkeypatch.setattr(gd.GDriveUploader, "_media_body", lambda self, path: None)
    uploader = gd.GDriveUploader(
        folder_id="folder-xyz", creds_json='{"x": 1}',
        service_factory=lambda creds: fake, make_public=False,
    )
    result = uploader.upload(workbook)
    assert result.uploaded is True
    assert fake.permissions_calls == []  # no public-link grant


# ---------------------------------------------------------------- failure handling

def test_upload_failure_returns_local_fallback_with_warning(monkeypatch, workbook):
    fake = _FakeDriveService(raise_on_create=True)
    monkeypatch.setattr(gd.GDriveUploader, "_media_body", lambda self, path: None)
    result = gd.upload_workbook(
        workbook,
        folder_id="folder-xyz",
        creds_json='{"x": 1}',
        service_factory=lambda creds: fake,
    )
    assert result.uploaded is False
    assert result.link.startswith("file://")
    assert "GDrive upload failed" in (result.warning or "")


# ---------------------------------------------------------------- credential path

def test_credential_payload_from_path(tmp_path):
    creds = {"project_id": "loop-ai", "client_email": "svc@loop.iam"}
    path = tmp_path / "svc.json"
    path.write_text(json.dumps(creds))
    loaded = gd._credential_payload(str(path))
    assert loaded == creds


def test_credential_payload_from_json_blob():
    loaded = gd._credential_payload('{"project_id": "inline"}')
    assert loaded == {"project_id": "inline"}


def test_credential_payload_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        gd._credential_payload(str(tmp_path / "missing.json"))
