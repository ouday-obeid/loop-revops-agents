"""O-gated merge of a snapshot file into canonical knowledge.

Flow:
1. O invokes `@oo revops-support knowledge merge 2026-04-20 sf_object_model.md`.
2. merger.merge(date, filename) copies the snapshot file over the canonical,
   git-commits the change in the canonical directory, and returns a
   MergeResult that the dispatcher replies with.
3. Caller is then expected to call `reingest.reingest_file(canonical_path)`.

The merger never writes to canonical without an explicit filename — no
bulk "merge everything" mode. Per safety rail: canonical `sf_*.md` is
never auto-overwritten.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agents.revops_support.knowledge_refresh.diff_producer import canonical_dir
from agents.revops_support.knowledge_refresh.metadata_snapshotter import (
    SNAPSHOT_FILES,
    _snapshots_root,
)

log = logging.getLogger(__name__)


class MergeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MergeResult:
    filename: str
    snapshot_path: Path
    canonical_path: Path
    git_committed: bool
    commit_sha: str | None
    message: str


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _commit_canonical(canonical: Path, filename: str, snapshot_date: str) -> tuple[bool, str | None]:
    """Commit `filename` to git if `canonical.parent` is a git repo. Returns (ok, sha)."""
    repo_dir = canonical.parent
    rc, _, _ = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
    if rc != 0:
        log.info("canonical dir is not a git repo; skipping commit")
        return False, None

    rc, _, err = _run_git(["add", str(canonical.name)], cwd=repo_dir)
    if rc != 0:
        raise MergeError(f"git add failed: {err}")

    rc, _, err = _run_git(["diff", "--cached", "--quiet"], cwd=repo_dir)
    if rc == 0:
        return False, None  # nothing to commit — canonical was identical to snapshot

    rc, _, err = _run_git(
        ["commit", "-m", f"knowledge refresh: merge {filename} from snapshot {snapshot_date}"],
        cwd=repo_dir,
    )
    if rc != 0:
        raise MergeError(f"git commit failed: {err}")
    rc, sha, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    return True, sha if rc == 0 else None


def merge(snapshot_date: str, filename: str, *, canonical: Path | None = None) -> MergeResult:
    """Copy `<snapshot_date>/<filename>` over canonical and git-commit.

    Raises MergeError if the snapshot is missing or the filename is not in
    SNAPSHOT_FILES. Missing canonical file is allowed — the merge creates it.
    """
    if filename not in SNAPSHOT_FILES:
        raise MergeError(
            f"refusing to merge '{filename}'; allowed: {', '.join(SNAPSHOT_FILES)}"
        )

    snap_dir = _snapshots_root() / snapshot_date
    snap_path = snap_dir / filename
    if not snap_path.exists():
        raise MergeError(f"snapshot not found: {snap_path}")

    canon_root = canonical or canonical_dir()
    canon_root.mkdir(parents=True, exist_ok=True)
    canon_path = canon_root / filename
    shutil.copy2(snap_path, canon_path)
    log.info("copied %s → %s", snap_path, canon_path)

    committed, sha = _commit_canonical(canon_path, filename, snapshot_date)
    msg = (
        f"merged {filename} from {snapshot_date}; commit {sha[:8]}" if (committed and sha)
        else f"merged {filename} from {snapshot_date}; no git commit (no repo or no diff)"
    )
    return MergeResult(
        filename=filename,
        snapshot_path=snap_path,
        canonical_path=canon_path,
        git_committed=committed,
        commit_sha=sha,
        message=msg,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Merge a snapshot file into canonical knowledge")
    ap.add_argument("--date", required=True)
    ap.add_argument("--file", required=True, choices=list(SNAPSHOT_FILES))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = merge(args.date, args.file)
    print(r.message)
    print(f"canonical: {r.canonical_path}")
