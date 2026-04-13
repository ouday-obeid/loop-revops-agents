"""Cloud Portability Rule #1: no hardcoded /Users/ paths in shared/ or agents/."""
import re
from pathlib import Path


def test_no_hardcoded_user_paths():
    root = Path(__file__).resolve().parents[1]
    bad = []
    pattern = re.compile(r"/Users/(ottimate|jarvis)/")
    for src_dir in [root / "shared", root / "agents"]:
        for p in src_dir.rglob("*.py"):
            text = p.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                if "/Users/" in line and pattern.search(line):
                    bad.append(f"{p}:{i}: {line.strip()}")
    assert not bad, "Hardcoded user paths found:\n" + "\n".join(bad)
