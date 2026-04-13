"""Cover knowledge_mcp ChromaBackend with a temp path + launchd renderer paths."""
import os
import tempfile
from pathlib import Path


def test_chroma_backend_ingest_and_search(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="revops_chroma_")
    monkeypatch.setenv("REVOPS_REPO_ROOT", tmp)
    from shared.mcp import knowledge_mcp
    knowledge_mcp._backend = None  # reset singleton
    doc_id = knowledge_mcp.ingest_document(
        "The TLO hierarchy maps ultimate parent accounts.",
        {"id": "tlo_doc", "source": "test"},
        corpus="sf_admin",
    )
    assert doc_id == "tlo_doc"
    hits = knowledge_mcp.semantic_search("TLO hierarchy", corpus="sf_admin", k=3)
    assert len(hits) >= 1
    assert hits[0]["id"] == "tlo_doc"
    got = knowledge_mcp.get_document("tlo_doc", corpus="sf_admin")
    assert "TLO" in got["content"]
    corpora = knowledge_mcp.list_corpora()
    assert "sf_admin" in corpora
    knowledge_mcp._backend = None


def test_launchd_render_reboot_job():
    from shared.runtime.launchd.generate import render
    from shared.runtime.schedule import by_name
    job = by_name("oo-daemon")
    xml = render(job, "/tmp/r", "/tmp/r/.venv/bin/python", "/tmp/r/var/log")
    assert "RunAtLoad" in xml
    assert "KeepAlive" in xml


def test_launchd_render_daily():
    from shared.runtime.launchd.generate import render
    from shared.runtime.schedule import by_name
    job = by_name("oo-briefing-daily")
    xml = render(job, "/tmp/r", "/tmp/r/.venv/bin/python", "/tmp/r/var/log")
    assert "Hour" in xml
    assert "Minute" in xml


def test_launchd_render_range():
    # weekday range 1-5 in daily briefing must expand
    from shared.runtime.launchd.generate import _parse_cron, _intervals_xml
    parsed = _parse_cron("30 8 * * 1-5")
    xml = _intervals_xml(parsed)
    # 5 weekday entries × all combined — ensure at least 5 <dict> entries
    assert xml.count("<dict>") >= 5
