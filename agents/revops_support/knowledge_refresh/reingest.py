"""Re-ingest a canonical knowledge file into chromadb after O-approved merge.

Uses `knowledge_mcp.ingest_chunked_document` with stable doc IDs of the form
`sf_admin/<basename>` so re-ingest is idempotent. Orphan chunks left by
shorter versions are pruned via `delete_stale_chunks`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from shared.mcp import knowledge_mcp

log = logging.getLogger(__name__)

DEFAULT_CORPUS = "sf_admin"
DEFAULT_CHUNK_SIZE = 2000
DEFAULT_OVERLAP = 200


@dataclass(frozen=True)
class ReingestResult:
    path: Path
    doc_id: str
    corpus: str
    chunks_written: int
    chunks_pruned: int


def _doc_id_for(path: Path) -> str:
    return f"sf_admin/{path.stem}"


def reingest_file(
    path: Path,
    *,
    corpus: str = DEFAULT_CORPUS,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> ReingestResult:
    if not path.exists():
        raise FileNotFoundError(path)

    content = path.read_text(encoding="utf-8")
    doc_id = _doc_id_for(path)
    metadata = {
        "id": doc_id,
        "source": "sf_admin",
        "filename": path.name,
        "origin_path": str(path),
    }
    chunk_ids = knowledge_mcp.ingest_chunked_document(
        content,
        metadata=metadata,
        corpus=corpus,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    pruned = knowledge_mcp.delete_stale_chunks(doc_id, chunk_ids, corpus=corpus)
    log.info(
        "reingested %s → %d chunks, pruned %d stale (corpus=%s)",
        path.name, len(chunk_ids), pruned, corpus,
    )
    return ReingestResult(
        path=path,
        doc_id=doc_id,
        corpus=corpus,
        chunks_written=len(chunk_ids),
        chunks_pruned=pruned,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Reingest a canonical knowledge file")
    ap.add_argument("path")
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = reingest_file(
        Path(args.path),
        corpus=args.corpus,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
    )
    print(f"wrote {r.chunks_written} chunks, pruned {r.chunks_pruned} (doc_id={r.doc_id})")
