"""Secrets access — dotenv locally, GCP Secret Manager in Phase 4.

Agent code MUST use get_secret() and never read os.environ for credentials directly.
Switch backends by setting REVOPS_SECRETS_BACKEND=gcp_secret_manager.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    root = os.environ.get("REVOPS_REPO_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def _load_dotenv_once() -> None:
    env_path = _repo_root() / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _backend() -> str:
    _load_dotenv_once()
    return os.environ.get("REVOPS_SECRETS_BACKEND", "dotenv")


def get_secret(name: str, default: str | None = None) -> str | None:
    """Resolve a secret by name. Backend-agnostic."""
    backend = _backend()
    if backend == "dotenv":
        return os.environ.get(name, default)
    if backend == "gcp_secret_manager":
        return _get_gcp_secret(name) or default
    raise ValueError(f"Unknown REVOPS_SECRETS_BACKEND: {backend}")


def require_secret(name: str) -> str:
    val = get_secret(name)
    if not val:
        raise RuntimeError(f"Required secret missing: {name}")
    return val


def get_config(name: str, default: str | None = None) -> str | None:
    """Non-sensitive config (paths, flags). Also env-var backed."""
    _load_dotenv_once()
    return os.environ.get(name, default)


def _get_gcp_secret(name: str) -> str | None:  # pragma: no cover - Phase 4
    try:
        from google.cloud import secretmanager
    except ImportError as e:
        raise RuntimeError("google-cloud-secret-manager not installed; pip install .[gcp]") from e
    project = os.environ["GCP_PROJECT"]
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{name}/versions/latest"
    resp = client.access_secret_version(request={"name": path})
    return resp.payload.data.decode("utf-8")
