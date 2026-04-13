"""Generate launchd plists from shared/runtime/schedule.py.

Usage:
  python -m shared.runtime.launchd.generate [--out DIR]

Writes com.loop-revops.<name>.plist for each Job.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from shared.runtime.schedule import SCHEDULE, Job

PLIST_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
"""


def _parse_cron(cron: str) -> list[dict] | str:
    if cron == "@reboot":
        return "reboot"
    m, h, dom, mon, dow = cron.split()
    entry: dict = {}

    def add(field: str, vals: str):
        if vals == "*":
            return
        if vals.startswith("*/"):
            step = int(vals[2:])
            limits = {"Minute": 60, "Hour": 24}
            if field in limits:
                # Key stays singular — launchd rejects plural forms (`Minutes`,
                # `Weekdays`) silently, so jobs would never fire on the Mac Mini.
                # `_intervals_xml` detects list vs scalar by value type, not key name.
                entry[field] = [i for i in range(0, limits[field], step)]
            return
        if "-" in vals:
            lo, hi = (int(x) for x in vals.split("-"))
            entry[field] = list(range(lo, hi + 1))
            return
        entry[field] = int(vals)

    add("Minute", m)
    add("Hour", h)
    add("Day", dom)
    add("Month", mon)
    add("Weekday", dow)
    # launchd's StartCalendarInterval wants lists to become multiple entries
    return [entry] if entry else [{}]


def _intervals_xml(parsed) -> str:
    if parsed == "reboot":
        return "<key>RunAtLoad</key><true/>\n<key>KeepAlive</key><true/>"
    # expand any list-valued field into multiple dict entries (launchd requirement)
    expanded: list[dict] = []
    for e in parsed:
        keys_with_lists = [k for k, v in e.items() if isinstance(v, list)]
        if not keys_with_lists:
            expanded.append(e)
            continue
        # single expansion level — enough for our cron expressions
        k = keys_with_lists[0]
        for v in e[k]:
            cp = {kk: vv for kk, vv in e.items() if kk != k}
            cp[k] = v
            expanded.append(cp)

    inner = ""
    for e in expanded:
        inner += "<dict>"
        for k, v in e.items():
            inner += f"<key>{k}</key><integer>{v}</integer>"
        inner += "</dict>"
    return f"<key>StartCalendarInterval</key><array>{inner}</array>"


def render(job: Job, repo_root: str, python_bin: str, log_dir: str) -> str:
    module, func = job.callable_path.split(":")
    label = f"com.loop-revops.{job.name}"
    program = (
        f"{python_bin} -c "
        f"\"import importlib,asyncio; "
        f"m=importlib.import_module('{module}'); "
        f"f=getattr(m,'{func}'); "
        f"r=f(); "
        f"asyncio.run(r) if hasattr(r,'__await__') else r\""
    )
    intervals = _intervals_xml(_parse_cron(job.cron))
    return (
        PLIST_HEADER
        + f"<key>Label</key><string>{label}</string>\n"
        + f"<key>ProgramArguments</key><array>"
          f"<string>/bin/sh</string><string>-c</string><string>{program}</string></array>\n"
        + f"<key>WorkingDirectory</key><string>{repo_root}</string>\n"
        + f"<key>EnvironmentVariables</key><dict>"
          f"<key>REVOPS_REPO_ROOT</key><string>{repo_root}</string>"
          f"<key>PYTHONPATH</key><string>{repo_root}</string></dict>\n"
        + f"<key>StandardOutPath</key><string>{log_dir}/{job.name}.out.log</string>\n"
        + f"<key>StandardErrorPath</key><string>{log_dir}/{job.name}.err.log</string>\n"
        + intervals
        + "\n</dict></plist>\n"
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=None, help="output directory for .plist files")
    p.add_argument("--repo-root", default=os.environ.get("REVOPS_REPO_ROOT") or os.getcwd())
    p.add_argument("--python", default=None, help="path to venv python")
    args = p.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    python_bin = args.python or f"{repo_root}/.venv/bin/python"
    log_dir = f"{repo_root}/var/log"
    out_dir = Path(args.out or f"{repo_root}/var/launchd")
    out_dir.mkdir(parents=True, exist_ok=True)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    for job in SCHEDULE:
        plist = render(job, repo_root, python_bin, log_dir)
        target = out_dir / f"com.loop-revops.{job.name}.plist"
        target.write_text(plist)
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
