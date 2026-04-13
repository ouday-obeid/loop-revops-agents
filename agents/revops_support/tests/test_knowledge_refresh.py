"""Tests for knowledge_refresh: snapshot, diff, merge, reingest."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


# ---------- metadata_snapshotter ----------

def test_snapshot_writes_three_files(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))

    from agents.revops_support.knowledge_refresh import metadata_snapshotter as ms

    # Stub the sf_* helpers so we don't hit the real CLI.
    # `sf sobject list --sobject custom` returns a flat list of API names.
    fake_sobject_names = ["TLO__c"]

    def fake_sf(*args, **kwargs):
        if args[:2] == ("org", "describe"):
            return {}
        if args[:2] == ("sobject", "list"):
            return fake_sobject_names
        return {}

    def fake_describe(name):
        return {
            "label": f"{name} label",
            "createable": True,
            "updateable": True,
            "deletable": False,
            "fields": [
                {"name": "Custom_Field__c", "label": "Custom", "type": "text",
                 "custom": True, "nillable": True, "createable": True},
                {"name": "Name", "label": "Name", "type": "string", "custom": False},
            ],
        }

    def fake_tooling(soql, *_a, **_kw):
        # Per-rule Metadata fetch (goes first since it contains "Metadata FROM ValidationRule").
        if "Metadata FROM ValidationRule" in soql:
            return {"records": [
                {"Metadata": {"errorConditionFormula": "ISBLANK(TLO__c)"}},
            ]}
        if "ValidationRule" in soql:
            return {"records": [
                {"Id": "03dVR001",
                 "ValidationName": "Require_TLO",
                 "Active": True,
                 "Description": "Ensure TLO is set",
                 "ErrorMessage": "TLO required"},
            ]}
        if "FROM Flow" in soql:
            return {"records": [
                {"MasterLabel": "Opp Stage Flow", "Status": "Active", "ProcessType": "AutoLaunchedFlow"},
            ]}
        if "ApexTrigger" in soql:
            return {"records": [
                {"Name": "AccountTrigger", "TableEnumOrId": "Account", "Status": "Active"},
            ]}
        return {"records": []}

    def fake_soql(query, limit=100):
        if "FROM User" in query:
            return {"records": [
                {"Name": "Alice", "Username": "alice@x.com",
                 "UserRole": {"Name": "Sales"}, "Profile": {"Name": "Standard"}},
            ]}
        if "FROM UserRole" in query:
            return {"records": [
                {"DeveloperName": "Sales", "Name": "Sales", "ParentRoleId": None},
            ]}
        if "FROM Profile" in query:
            return {"records": [{"Name": "Standard", "UserType": "Standard"}]}
        return {"records": []}

    monkeypatch.setattr(ms.salesforce_mcp, "_sf", fake_sf)
    monkeypatch.setattr(ms.salesforce_mcp, "describe_sobject", fake_describe)
    monkeypatch.setattr(ms.salesforce_mcp, "tooling_query", fake_tooling)
    monkeypatch.setattr(ms.salesforce_mcp, "soql_query", fake_soql)

    paths = ms.snapshot(target_date="2026-04-20")
    assert set(paths) == {"object_model", "automations", "users_roles"}
    for p in paths.values():
        assert p.exists() and p.stat().st_size > 0

    object_model = paths["object_model"].read_text()
    assert "TLO__c" in object_model
    assert "Custom_Field__c" in object_model
    assert "Require_TLO" in object_model
    assert "ISBLANK(TLO__c)" in object_model

    automations = paths["automations"].read_text()
    assert "Opp Stage Flow" in automations
    assert "AccountTrigger" in automations

    users = paths["users_roles"].read_text()
    assert "Alice" in users
    assert "Sales" in users
    assert "Standard" in users


def test_snapshot_survives_partial_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.knowledge_refresh import metadata_snapshotter as ms

    def boom(*a, **kw):
        raise RuntimeError("sf cli blew up")

    # Break everything except the toolkit's _safe() wrapper.
    monkeypatch.setattr(ms.salesforce_mcp, "_sf", boom)
    monkeypatch.setattr(ms.salesforce_mcp, "describe_sobject", boom)
    monkeypatch.setattr(ms.salesforce_mcp, "tooling_query", boom)
    monkeypatch.setattr(ms.salesforce_mcp, "soql_query", boom)

    paths = ms.snapshot(target_date="2026-04-20")
    for p in paths.values():
        assert p.exists()
        text = p.read_text()
        assert text.startswith("# ")  # still produces markdown scaffold


# ---------- diff_producer ----------

def test_diff_producer_groups_changes_by_heading():
    from agents.revops_support.knowledge_refresh import diff_producer

    old = (
        "# Doc\n\n## A\nalpha\n\n## B\nold beta line\n"
    )
    new = (
        "# Doc\n\n## A\nalpha\n\n## B\nnew beta line\n\n## C\ngamma\n"
    )
    sections = diff_producer.diff(old, new)
    paths_changed = {s.path for s in sections}
    # A is unchanged; B and C should appear.
    assert any("## B" in " ".join(p) for p in paths_changed)
    assert any("## C" in " ".join(p) for p in paths_changed)
    assert not any(p == ("# Doc", "## A") for p in paths_changed)


def test_diff_producer_handles_missing_canonical(tmp_path):
    from agents.revops_support.knowledge_refresh import diff_producer

    snap = tmp_path / "sf_object_model.md"
    snap.write_text("# SF\n## Account\nline\n")
    canon_missing = tmp_path / "missing.md"

    sections = diff_producer.diff_files(snap, canon_missing)
    assert sections, "missing canonical should yield additions"
    assert all(s.added and not s.removed for s in sections)


def test_render_summary_no_changes(tmp_path):
    from agents.revops_support.knowledge_refresh import diff_producer

    snap_dir = tmp_path / "2026-04-20"
    canon_dir = tmp_path / "canon"
    snap_dir.mkdir()
    canon_dir.mkdir()
    for fn in ("sf_object_model.md", "sf_automations.md", "sf_users_roles.md"):
        (snap_dir / fn).write_text(f"# {fn}\nbody\n")
        (canon_dir / fn).write_text(f"# {fn}\nbody\n")

    summary = diff_producer.render_summary(
        snap_dir, canon_dir,
        ("sf_object_model.md", "sf_automations.md", "sf_users_roles.md"),
    )
    # Nothing changed across any file — expect the consolidated escape hatch,
    # not three per-file "no changes" entries.
    assert "_No sections changed week-over-week._" in summary
    assert "## sf_object_model.md" not in summary


# ---------- merger ----------

def test_merge_copies_and_commits(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("REVOPS_CANONICAL_KNOWLEDGE_DIR", str(tmp_path / "canon"))

    from agents.revops_support.knowledge_refresh import merger, metadata_snapshotter as ms

    snap_dir = ms._snapshots_root() / "2026-04-20"
    snap_dir.mkdir(parents=True)
    (snap_dir / "sf_object_model.md").write_text("# SF\n## Account\nline1\n")

    canon_dir = tmp_path / "canon"
    canon_dir.mkdir()
    # Initialize a git repo on the canonical dir.
    subprocess.run(["git", "init", "-q"], cwd=canon_dir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "seed", "-q"], cwd=canon_dir, check=True)

    r = merger.merge("2026-04-20", "sf_object_model.md")
    assert r.canonical_path.read_text().startswith("# SF")
    assert r.git_committed is True
    assert r.commit_sha and len(r.commit_sha) >= 7


def test_merge_no_git_repo_still_copies(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("REVOPS_CANONICAL_KNOWLEDGE_DIR", str(tmp_path / "plain"))

    from agents.revops_support.knowledge_refresh import merger, metadata_snapshotter as ms

    snap_dir = ms._snapshots_root() / "2026-04-20"
    snap_dir.mkdir(parents=True)
    (snap_dir / "sf_automations.md").write_text("# A\nbody\n")

    r = merger.merge("2026-04-20", "sf_automations.md")
    assert r.canonical_path.exists()
    assert r.git_committed is False
    assert r.commit_sha is None


def test_merge_rejects_unknown_filename(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.knowledge_refresh import merger

    with pytest.raises(merger.MergeError):
        merger.merge("2026-04-20", "arbitrary.md")


def test_merge_rejects_missing_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("REVOPS_CANONICAL_KNOWLEDGE_DIR", str(tmp_path / "canon"))
    from agents.revops_support.knowledge_refresh import merger

    with pytest.raises(merger.MergeError):
        merger.merge("1999-01-01", "sf_object_model.md")


# ---------- reingest ----------

def test_reingest_file_is_idempotent(monkeypatch, tmp_path):
    """Reingesting the same file twice leaves the same number of chunks."""
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from shared.mcp import knowledge_mcp
    knowledge_mcp._backend = None

    from agents.revops_support.knowledge_refresh import reingest

    canon = tmp_path / "sf_object_model.md"
    canon.write_text(
        "# SF Object Model\n\n## Account\nline one\n\n## Opportunity\nline two\n"
    )
    r1 = reingest.reingest_file(canon, corpus="reingest_idem")
    r2 = reingest.reingest_file(canon, corpus="reingest_idem")

    assert r1.chunks_written == r2.chunks_written
    assert r2.chunks_pruned == 0
    remaining = knowledge_mcp.list_document_ids("reingest_idem", prefix=r1.doc_id + "#")
    assert len(remaining) == r1.chunks_written
    knowledge_mcp._backend = None


def test_reingest_shrinks_and_prunes(monkeypatch, tmp_path):
    """A smaller re-ingest prunes stale chunks from the previous version."""
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from shared.mcp import knowledge_mcp
    knowledge_mcp._backend = None

    from agents.revops_support.knowledge_refresh import reingest

    big = (
        "# Doc\n\n" + "\n\n".join(
            f"## Section {i}\n" + ("body " * 100)
            for i in range(8)
        )
    )
    path = tmp_path / "sf_object_model.md"
    path.write_text(big)
    r_big = reingest.reingest_file(path, corpus="reingest_shrink", chunk_size=400, overlap=40)

    path.write_text("# Doc\n\n## Section 0\nsmall\n")
    r_small = reingest.reingest_file(path, corpus="reingest_shrink", chunk_size=400, overlap=40)

    assert r_small.chunks_written < r_big.chunks_written
    assert r_small.chunks_pruned == r_big.chunks_written - r_small.chunks_written
    remaining = knowledge_mcp.list_document_ids("reingest_shrink", prefix=r_big.doc_id + "#")
    assert len(remaining) == r_small.chunks_written
    knowledge_mcp._backend = None


def test_reingest_file_missing(tmp_path):
    from agents.revops_support.knowledge_refresh import reingest

    with pytest.raises(FileNotFoundError):
        reingest.reingest_file(tmp_path / "nope.md")
