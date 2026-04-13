"""Knowledge MCP — RAG over project corpora.

Pluggable backend: `chromadb_local` (Phase 0) or `pgvector` (Phase 4 stub).
Embedding model: all-MiniLM-L6-v2 (matches OUTBOUNDER for consistency).
Default corpora path: ${REVOPS_REPO_ROOT}/var/chroma/
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from shared.secrets import get_config

log = logging.getLogger(__name__)

_EMBED_MODEL = "all-MiniLM-L6-v2"


class KnowledgeBackend(Protocol):
    def semantic_search(self, query: str, corpus: str, k: int) -> list[dict[str, Any]]: ...
    def get_document(self, doc_id: str, corpus: str) -> dict[str, Any]: ...
    def ingest_document(self, content: str, metadata: dict, corpus: str) -> str: ...
    def list_corpora(self) -> list[str]: ...
    def list_document_ids(self, corpus: str, prefix: str | None = None) -> list[str]: ...
    def delete_documents(self, ids: list[str], corpus: str) -> int: ...


class ChromaBackend:
    def __init__(self, path: str | None = None):
        import chromadb

        root = get_config("REVOPS_REPO_ROOT") or str(Path(__file__).resolve().parents[2])
        store = Path(path or f"{root}/var/chroma")
        store.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(store))
        self._embed = None
        try:
            from chromadb.utils import embedding_functions
            self._embed = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=_EMBED_MODEL
            )
        except (ImportError, ValueError):
            log.warning("sentence-transformers not installed; using chroma default embedding")

    def _col(self, corpus: str):
        if self._embed is not None:
            return self._client.get_or_create_collection(name=corpus, embedding_function=self._embed)
        return self._client.get_or_create_collection(name=corpus)

    def semantic_search(self, query: str, corpus: str, k: int) -> list[dict[str, Any]]:
        col = self._col(corpus)
        res = col.query(query_texts=[query], n_results=k)
        out: list[dict[str, Any]] = []
        for i, doc in enumerate(res.get("documents", [[]])[0]):
            out.append({
                "id": res["ids"][0][i],
                "content": doc,
                "metadata": (res.get("metadatas") or [[{}]])[0][i],
                "distance": (res.get("distances") or [[None]])[0][i],
            })
        return out

    def get_document(self, doc_id: str, corpus: str) -> dict[str, Any]:
        col = self._col(corpus)
        res = col.get(ids=[doc_id])
        if not res["ids"]:
            return {}
        return {
            "id": res["ids"][0],
            "content": res["documents"][0],
            "metadata": res["metadatas"][0] if res["metadatas"] else {},
        }

    def ingest_document(self, content: str, metadata: dict, corpus: str) -> str:
        col = self._col(corpus)
        doc_id = metadata.get("id") or str(uuid.uuid4())
        col.upsert(ids=[doc_id], documents=[content], metadatas=[metadata or {"_": ""}])
        return doc_id

    def list_corpora(self) -> list[str]:
        return [c.name for c in self._client.list_collections()]

    def list_document_ids(self, corpus: str, prefix: str | None = None) -> list[str]:
        col = self._col(corpus)
        res = col.get(include=[])
        ids: list[str] = list(res.get("ids") or [])
        if prefix:
            ids = [i for i in ids if i.startswith(prefix)]
        return ids

    def delete_documents(self, ids: list[str], corpus: str) -> int:
        if not ids:
            return 0
        col = self._col(corpus)
        col.delete(ids=ids)
        return len(ids)


class PgVectorBackend:  # pragma: no cover - Phase 4 stub
    def __init__(self, dsn: str | None = None):
        raise NotImplementedError("pgvector backend is a Phase 4 deliverable")

    def semantic_search(self, *a, **kw): raise NotImplementedError
    def get_document(self, *a, **kw): raise NotImplementedError
    def ingest_document(self, *a, **kw): raise NotImplementedError
    def list_corpora(self): raise NotImplementedError
    def list_document_ids(self, *a, **kw): raise NotImplementedError
    def delete_documents(self, *a, **kw): raise NotImplementedError


_backend: KnowledgeBackend | None = None


def _get_backend() -> KnowledgeBackend:
    global _backend
    if _backend is not None:
        return _backend
    kind = get_config("REVOPS_KNOWLEDGE_BACKEND") or "chromadb_local"
    if kind == "chromadb_local":
        _backend = ChromaBackend()
    elif kind == "pgvector":
        _backend = PgVectorBackend()
    else:
        raise ValueError(f"Unknown REVOPS_KNOWLEDGE_BACKEND: {kind}")
    return _backend


def semantic_search(query: str, corpus: str = "default", k: int = 5) -> list[dict[str, Any]]:
    return _get_backend().semantic_search(query, corpus, k)


def get_document(doc_id: str, corpus: str = "default") -> dict[str, Any]:
    return _get_backend().get_document(doc_id, corpus)


def ingest_document(content: str, metadata: dict, corpus: str = "default") -> str:
    return _get_backend().ingest_document(content, metadata, corpus)


def list_corpora() -> list[str]:
    return _get_backend().list_corpora()


def list_document_ids(corpus: str = "default", prefix: str | None = None) -> list[str]:
    return _get_backend().list_document_ids(corpus, prefix)


def delete_documents(ids: list[str], corpus: str = "default") -> int:
    return _get_backend().delete_documents(ids, corpus)


_HEADING_RE = re.compile(r"^(#{1,6})\s+")


def _chunk_markdown(content: str, chunk_size: int, overlap: int) -> list[str]:
    """Split markdown into chunks; each chunk prefixed with its ancestor heading stack.

    Algorithm:
    1. Walk line-by-line. When we hit a heading, flush the current body as a segment
       tied to the prior heading stack, then update the stack for the new heading.
    2. For each segment, build heading_ctx (joined ancestor headings) + body.
    3. If the combined chunk exceeds chunk_size, sub-split the body on line
       boundaries with `overlap` characters repeated between adjacent sub-chunks.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")

    segments: list[tuple[list[str], list[str]]] = []
    stack: list[str] = []
    body: list[str] = []
    for line in content.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            segments.append((list(stack), body))
            level = len(m.group(1))
            trimmed = stack[: level - 1]
            while len(trimmed) < level - 1:
                trimmed.append("")
            trimmed.append(line.rstrip())
            stack = trimmed
            body = []
        else:
            body.append(line)
    segments.append((list(stack), body))

    chunks: list[str] = []
    for heading_stack, body_lines in segments:
        heading_ctx = "\n".join(h for h in heading_stack if h).strip()
        body_text = "\n".join(body_lines).strip()
        if not body_text:
            continue

        prefix = f"{heading_ctx}\n\n" if heading_ctx else ""
        prefix_len = len(prefix)
        avail = max(1, chunk_size - prefix_len)

        if len(body_text) <= avail:
            chunks.append(f"{prefix}{body_text}")
            continue

        start = 0
        while start < len(body_text):
            end = min(start + avail, len(body_text))
            if end < len(body_text):
                nl = body_text.rfind("\n", start, end)
                if nl > start + avail // 2:
                    end = nl
            piece = body_text[start:end].strip()
            if piece:
                chunks.append(f"{prefix}{piece}")
            if end >= len(body_text):
                break
            start = max(end - overlap, start + 1)

    return [c for c in chunks if c.strip()]


def ingest_chunked_document(
    content: str,
    metadata: dict,
    corpus: str = "default",
    chunk_size: int = 2000,
    overlap: int = 200,
) -> list[str]:
    """Ingest a markdown doc as heading-aware chunks. Returns list of chunk IDs.

    `metadata['id']` is required — it's the stable doc_id used to build chunk
    IDs like `{doc_id}#{NNNN}`. Callers re-ingesting a document should pair
    this with `delete_stale_chunks(doc_id, keep_ids=<returned>)` to prune
    chunks that no longer exist in the new version.
    """
    doc_id = metadata.get("id")
    if not doc_id:
        raise ValueError("metadata['id'] is required for chunked ingest")

    pieces = _chunk_markdown(content, chunk_size=chunk_size, overlap=overlap)
    backend = _get_backend()
    chunk_ids: list[str] = []
    total = len(pieces)
    for i, piece in enumerate(pieces):
        cid = f"{doc_id}#{i:04d}"
        meta = {
            **(metadata or {}),
            "id": cid,
            "doc_id": doc_id,
            "chunk_idx": i,
            "total_chunks": total,
        }
        backend.ingest_document(piece, meta, corpus)
        chunk_ids.append(cid)
    return chunk_ids


def delete_stale_chunks(doc_id_prefix: str, keep_ids: list[str], corpus: str = "default") -> int:
    """Delete chunks whose IDs begin with `{doc_id_prefix}#` but aren't in keep_ids.

    Re-ingesting a smaller version of a previously-chunked document leaves
    orphan chunks behind; this prunes them. Scoped by the `#` separator so a
    prefix of `sf_object_model` doesn't accidentally match `sf_object_model_v2`.
    """
    scoped = f"{doc_id_prefix}#"
    existing = _get_backend().list_document_ids(corpus, prefix=scoped)
    keep = set(keep_ids)
    stale = [i for i in existing if i not in keep]
    return _get_backend().delete_documents(stale, corpus)


def _smoke() -> None:
    hits = semantic_search("TLO hierarchy", corpus="sf_admin", k=3)
    print(f"knowledge hits: {len(hits)}")


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        _smoke()
