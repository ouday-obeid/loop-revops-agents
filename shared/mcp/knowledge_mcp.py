"""Knowledge MCP — RAG over project corpora.

Pluggable backend: `chromadb_local` (Phase 0) or `pgvector` (Phase 4 stub).
Embedding model: all-MiniLM-L6-v2 (matches OUTBOUNDER for consistency).
Default corpora path: ${REVOPS_REPO_ROOT}/var/chroma/
"""
from __future__ import annotations

import logging
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


class PgVectorBackend:  # pragma: no cover - Phase 4 stub
    def __init__(self, dsn: str | None = None):
        raise NotImplementedError("pgvector backend is a Phase 4 deliverable")

    def semantic_search(self, *a, **kw): raise NotImplementedError
    def get_document(self, *a, **kw): raise NotImplementedError
    def ingest_document(self, *a, **kw): raise NotImplementedError
    def list_corpora(self): raise NotImplementedError


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


def _smoke() -> None:
    hits = semantic_search("TLO hierarchy", corpus="sf_admin", k=3)
    print(f"knowledge hits: {len(hits)}")


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        _smoke()
