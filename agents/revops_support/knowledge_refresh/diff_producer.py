"""Heading-level markdown diff between a snapshot and its canonical file.

Two markdown files → one summary that groups line-level changes under their
innermost heading. The agent DMs the summary to O on Monday 09:00 so she can
decide which sections to `merge`.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class SectionDiff:
    path: tuple[str, ...]       # full heading stack, e.g. ("# SF Object Model", "## Account")
    added: list[str]
    removed: list[str]
    changed: bool               # True if either add/remove

    def as_markdown(self) -> str:
        header = " › ".join(self.path) or "(document root)"
        out = [f"### {header}"]
        if self.added:
            out.append(f"_+{len(self.added)} lines added_")
            for ln in self.added[:10]:
                out.append(f"  + {ln}")
            if len(self.added) > 10:
                out.append(f"  + …and {len(self.added) - 10} more")
        if self.removed:
            out.append(f"_-{len(self.removed)} lines removed_")
            for ln in self.removed[:10]:
                out.append(f"  - {ln}")
            if len(self.removed) > 10:
                out.append(f"  - …and {len(self.removed) - 10} more")
        return "\n".join(out)


def _walk_headings(lines: Iterable[str]) -> list[tuple[int, tuple[str, ...], list[str]]]:
    """Return (line_index_of_heading_start, heading_stack, body_lines) per section.

    The first "section" (lines before the first heading) gets stack=().
    """
    sections: list[tuple[int, tuple[str, ...], list[str]]] = []
    stack: list[str] = []
    body: list[str] = []
    section_start = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            sections.append((section_start, tuple(stack), list(body)))
            level = len(m.group(1))
            trimmed = stack[: level - 1]
            while len(trimmed) < level - 1:
                trimmed.append("")
            trimmed.append(line.rstrip())
            stack = trimmed
            body = []
            section_start = i
        else:
            body.append(line)
    sections.append((section_start, tuple(stack), list(body)))
    return sections


def diff(old_text: str, new_text: str) -> list[SectionDiff]:
    """Return section-level diffs. Empty list = no changes."""
    old_sections = _walk_headings(old_text.splitlines())
    new_sections = _walk_headings(new_text.splitlines())

    by_path_old: dict[tuple[str, ...], list[str]] = {}
    for _, path, body in old_sections:
        by_path_old.setdefault(path, []).extend(body)
    by_path_new: dict[tuple[str, ...], list[str]] = {}
    for _, path, body in new_sections:
        by_path_new.setdefault(path, []).extend(body)

    all_paths: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for path in list(by_path_old.keys()) + list(by_path_new.keys()):
        if path not in seen:
            all_paths.append(path)
            seen.add(path)

    diffs: list[SectionDiff] = []
    for path in all_paths:
        old_body = [l for l in by_path_old.get(path, []) if l.strip()]
        new_body = [l for l in by_path_new.get(path, []) if l.strip()]
        if old_body == new_body:
            continue
        added: list[str] = []
        removed: list[str] = []
        sm = difflib.SequenceMatcher(a=old_body, b=new_body)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            removed.extend(old_body[i1:i2])
            added.extend(new_body[j1:j2])
        if added or removed:
            diffs.append(SectionDiff(path=path, added=added, removed=removed, changed=True))
    return diffs


def diff_files(snapshot_path: Path, canonical_path: Path) -> list[SectionDiff]:
    """Diff snapshot vs canonical. Missing canonical = everything is 'added'."""
    snap_text = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else ""
    canon_text = canonical_path.read_text(encoding="utf-8") if canonical_path.exists() else ""
    return diff(canon_text, snap_text)


def render_summary(snapshot_dir: Path, canonical_dir: Path, filenames: Iterable[str]) -> str:
    """One markdown block summarising changes across all files in a snapshot."""
    header = [f"# Knowledge Refresh — {snapshot_dir.name}", ""]
    body: list[str] = []
    any_changes = False
    for fn in filenames:
        snap = snapshot_dir / fn
        canon = canonical_dir / fn
        sections = diff_files(snap, canon)
        if not sections:
            body.append(f"## {fn}")
            body.append("_no changes_")
            body.append("")
            continue
        any_changes = True
        added_total = sum(len(s.added) for s in sections)
        removed_total = sum(len(s.removed) for s in sections)
        body.append(f"## {fn} (+{added_total} / -{removed_total})")
        body.append("")
        for s in sections:
            body.append(s.as_markdown())
            body.append("")
    if not any_changes:
        body = ["_No sections changed week-over-week._"]
    return "\n".join(header + body).rstrip() + "\n"


def canonical_dir() -> Path:
    """Where human-curated `sf_*.md` live. Configurable via REVOPS_CANONICAL_KNOWLEDGE_DIR."""
    from shared.secrets import get_config
    explicit = get_config("REVOPS_CANONICAL_KNOWLEDGE_DIR")
    if explicit:
        return Path(explicit)
    return Path.home() / "sf-admin" / "knowledge"


if __name__ == "__main__":
    import argparse
    from agents.revops_support.knowledge_refresh.metadata_snapshotter import (
        SNAPSHOT_FILES,
        _snapshots_root,
    )

    ap = argparse.ArgumentParser(description="Produce markdown diff of snapshot vs canonical")
    ap.add_argument("--date", required=True, help="Snapshot date YYYY-MM-DD")
    ap.add_argument("--canonical", default=None, help="Canonical knowledge dir override")
    args = ap.parse_args()

    snap_dir = _snapshots_root() / args.date
    canon_dir = Path(args.canonical) if args.canonical else canonical_dir()
    print(render_summary(snap_dir, canon_dir, SNAPSHOT_FILES))
