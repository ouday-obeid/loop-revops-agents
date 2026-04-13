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


def test_chunk_markdown_preserves_heading_stack():
    from shared.mcp.knowledge_mcp import _chunk_markdown

    doc = (
        "# Top\n"
        "Intro.\n\n"
        "## Section A\n"
        "Body A.\n\n"
        "### Subsection A1\n"
        "Body A1.\n\n"
        "## Section B\n"
        "Body B.\n"
    )
    chunks = _chunk_markdown(doc, chunk_size=500, overlap=20)
    # Every chunk that mentions a subsection must carry its ancestor headings.
    sub_chunk = next(c for c in chunks if "A1" in c)
    assert "# Top" in sub_chunk
    assert "## Section A" in sub_chunk
    assert "### Subsection A1" in sub_chunk
    # No empty heading-only chunks.
    for c in chunks:
        body_after_headings = "\n".join(
            line for line in c.split("\n") if not line.lstrip().startswith("#")
        ).strip()
        assert body_after_headings, f"empty body chunk: {c!r}"


def test_chunk_markdown_subsplits_oversized_body():
    from shared.mcp.knowledge_mcp import _chunk_markdown

    big_body = "\n".join(f"line {i} " * 10 for i in range(60))
    doc = f"# Root\n\n## Big\n\n{big_body}\n"
    chunks = _chunk_markdown(doc, chunk_size=400, overlap=50)
    assert len(chunks) > 1
    for c in chunks:
        assert c.startswith("# Root\n## Big"), c[:40]
        assert len(c) <= 400 + 50  # allow small slack from heading prefix


def test_chunk_markdown_rejects_bad_overlap():
    import pytest
    from shared.mcp.knowledge_mcp import _chunk_markdown

    with pytest.raises(ValueError):
        _chunk_markdown("# A\nbody", chunk_size=100, overlap=100)
    with pytest.raises(ValueError):
        _chunk_markdown("# A\nbody", chunk_size=0, overlap=0)


def test_ingest_chunked_document_and_prune(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="revops_chroma_chunked_")
    monkeypatch.setenv("REVOPS_REPO_ROOT", tmp)
    from shared.mcp import knowledge_mcp
    knowledge_mcp._backend = None

    doc = (
        "# SF Object Model\n\n"
        "## Account\n\nAccount notes.\n\n"
        "## Opportunity\n\nOpp notes.\n"
    )
    ids = knowledge_mcp.ingest_chunked_document(
        doc,
        {"id": "sf_admin/sf_object_model", "source": "snapshot"},
        corpus="sf_admin_chunks",
        chunk_size=500,
        overlap=40,
    )
    assert len(ids) >= 2
    assert all(i.startswith("sf_admin/sf_object_model#") for i in ids)

    # Re-ingest a smaller version; prune stale chunks.
    doc2 = "# SF Object Model\n\n## Account\n\nShorter.\n"
    ids2 = knowledge_mcp.ingest_chunked_document(
        doc2,
        {"id": "sf_admin/sf_object_model"},
        corpus="sf_admin_chunks",
        chunk_size=500,
        overlap=40,
    )
    pruned = knowledge_mcp.delete_stale_chunks(
        "sf_admin/sf_object_model", ids2, corpus="sf_admin_chunks"
    )
    assert pruned == len(ids) - len(ids2)

    remaining = knowledge_mcp.list_document_ids(
        "sf_admin_chunks", prefix="sf_admin/sf_object_model#"
    )
    assert sorted(remaining) == sorted(ids2)
    knowledge_mcp._backend = None


def test_ingest_chunked_document_requires_id(monkeypatch):
    import pytest
    tmp = tempfile.mkdtemp(prefix="revops_chroma_noid_")
    monkeypatch.setenv("REVOPS_REPO_ROOT", tmp)
    from shared.mcp import knowledge_mcp
    knowledge_mcp._backend = None
    with pytest.raises(ValueError):
        knowledge_mcp.ingest_chunked_document(
            "# A\nbody", {"source": "missing-id"}, corpus="x"
        )
    knowledge_mcp._backend = None


def test_delete_stale_chunks_is_prefix_scoped(monkeypatch):
    """`foo` must not match `foo_v2` — scoping on `#` separator."""
    tmp = tempfile.mkdtemp(prefix="revops_chroma_scope_")
    monkeypatch.setenv("REVOPS_REPO_ROOT", tmp)
    from shared.mcp import knowledge_mcp
    knowledge_mcp._backend = None

    knowledge_mcp.ingest_chunked_document(
        "# A\n\n## B\n\nBody B\n",
        {"id": "foo"},
        corpus="scope_corpus",
        chunk_size=500,
    )
    knowledge_mcp.ingest_chunked_document(
        "# A\n\n## B\n\nBody B\n",
        {"id": "foo_v2"},
        corpus="scope_corpus",
        chunk_size=500,
    )
    # Delete all of `foo`'s chunks; `foo_v2`'s chunks stay intact.
    knowledge_mcp.delete_stale_chunks("foo", [], corpus="scope_corpus")
    survivors = knowledge_mcp.list_document_ids("scope_corpus")
    assert all(s.startswith("foo_v2#") for s in survivors)
    assert any(s.startswith("foo_v2#") for s in survivors)
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
