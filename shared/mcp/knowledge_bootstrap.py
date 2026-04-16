"""Knowledge corpus seeding — pulls $SF_ADMIN_KNOWLEDGE_DIR/*.md
into the `sf_admin` corpus on agent boot.

Tier 10 of v0.7-hygiene plan (closes Monday subitems on parent
11736879435: 'Seed sf_admin corpus from sf-admin knowledge dir' +
'FIX: Install sentence-transformers; pin embedding model to
all-MiniLM-L6-v2 in knowledge_mcp.py').

Source dir is resolved from SF_ADMIN_KNOWLEDGE_DIR env var (no
hardcoded /Users/... default — that would violate cloud-portability
rule #1 for prod deploys). Operators set SF_ADMIN_KNOWLEDGE_DIR in
.env. Missing env var → no-op.

Idempotency: ingest_chunked_document keys chunks by `{doc_id}#{NNNN}`;
re-running the seed updates the same IDs in place (Chroma's upsert).
delete_stale_chunks prunes chunks that disappeared in the new version
(e.g. an .md file shrunk between runs).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from shared.mcp import knowledge_mcp
from shared.secrets import get_config

log = logging.getLogger(__name__)

CORPUS = "sf_admin"


def _doc_id(md_path: Path) -> str:
    """Stable doc_id derived from filename stem (no path/extension noise)."""
    return md_path.stem


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def seed_sf_admin_corpus(source_dir: str | None = None) -> dict[str, int]:
    """Ingest every *.md file under source_dir into the sf_admin corpus.

    Returns a summary {ingested_files, total_chunks, deleted_stale_chunks}.

    Source resolution order:
      1. Explicit `source_dir` argument (used by tests).
      2. `SF_ADMIN_KNOWLEDGE_DIR` env var.
      3. No-op (returns zeros) — no hardcoded /Users/... default so
         the prod build stays cloud-portable.

    Bootstrap failure on a single file does NOT abort the whole seed —
    log + continue.
    """
    summary = {"ingested_files": 0, "total_chunks": 0, "deleted_stale_chunks": 0}
    resolved = source_dir or get_config("SF_ADMIN_KNOWLEDGE_DIR")
    if not resolved:
        log.info("knowledge_bootstrap: SF_ADMIN_KNOWLEDGE_DIR unset — skipping seed")
        return summary
    src = Path(resolved)
    if not src.exists() or not src.is_dir():
        log.info("knowledge_bootstrap: %s does not exist — skipping seed", src)
        return summary

    md_files = sorted(src.glob("*.md"))
    if not md_files:
        log.info("knowledge_bootstrap: no .md files in %s", src)
        return summary

    for md in md_files:
        try:
            content = md.read_text(encoding="utf-8")
            doc_id = _doc_id(md)
            chunk_ids = knowledge_mcp.ingest_chunked_document(
                content,
                {
                    "id": doc_id,
                    "source": str(md),
                    "filename": md.name,
                    "content_hash": _content_hash(content),
                },
                corpus=CORPUS,
            )
            stale_pruned = knowledge_mcp.delete_stale_chunks(
                doc_id, keep_ids=chunk_ids, corpus=CORPUS
            )
            summary["ingested_files"] += 1
            summary["total_chunks"] += len(chunk_ids)
            summary["deleted_stale_chunks"] += stale_pruned
        except Exception as e:
            log.warning("knowledge_bootstrap: failed to ingest %s: %s", md, e)
    log.info(
        "knowledge_bootstrap: %d files, %d chunks, %d stale pruned",
        summary["ingested_files"], summary["total_chunks"],
        summary["deleted_stale_chunks"],
    )
    return summary


if __name__ == "__main__":
    print(seed_sf_admin_corpus())
