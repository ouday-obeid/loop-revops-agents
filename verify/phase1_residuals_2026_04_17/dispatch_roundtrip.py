"""@oo dispatcher round-trip harness — Phase 1 residuals verification.

Registers all 6 Phase 1 specialists + OO, then calls `shared.slack_dispatcher.dispatch`
with safe commands (ping, help) that exercise routing without touching SF. Captures
response text per agent for stamp evidence.

Run:
    cd ~/loop-revops-agents
    python3 -m verify.phase1_residuals_2026_04_17.dispatch_roundtrip

Artifact: verify/phase1_residuals_2026_04_17/dispatch_roundtrip_<timestamp>.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Keep dev guard on so any accidental send is blocked
os.environ.setdefault("SLACK_DEV_GUARD", "1")
os.environ.setdefault("SLACK_TEST_CHANNEL", "U08K2UTG3G8")

from shared import slack_dispatcher
from agents.oo import main as oo_main
from agents.sales_reps import main as sales_reps_main
from agents.revops_support import main as revops_main
from agents.slt_metrics import main as slt_main
from agents.top_of_funnel import main as tof_main
from agents.cs import main as cs_main
from agents.onboarding import main as onb_main

AGENTS = [
    ("oo", oo_main),
    ("sales_reps", sales_reps_main),
    ("revops_support", revops_main),
    ("slt_metrics", slt_main),
    ("top_of_funnel", tof_main),
    ("cs", cs_main),
    ("onboarding", onb_main),
]

PERSONA_CHECKS = [
    ("outbounder help", "top_of_funnel"),
    ("closer help", "sales_reps"),
    ("onboarder help", "onboarding"),
    ("supporter help", "cs"),
    ("admin help", "revops_support"),
    ("urkel help", "slt_metrics"),
]


def _register_all() -> list[str]:
    registered = []
    for name, mod in AGENTS:
        # Each agent picked one of several registration conventions during Phase 1:
        # sales_reps + revops_support + top_of_funnel + slt_metrics: register_with_dispatcher()
        # cs + onboarding: bootstrap()
        # oo: direct register against its inline dispatcher module
        reg_fn = getattr(mod, "register_with_dispatcher", None) or getattr(mod, "bootstrap", None)
        if reg_fn is not None:
            reg_fn()
            registered.append(name)
            continue
        if name == "oo":
            slack_dispatcher.register("oo", oo_main.oo_dispatcher.handle)
            registered.append(name)
            continue
        raise RuntimeError(f"{name} has no register_with_dispatcher() or bootstrap()")
    return registered


async def _probe(text_in: str, context: dict) -> dict:
    try:
        resp = await slack_dispatcher.dispatch(text_in, context)
        return {"ok": True, "response": resp}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def main() -> int:
    registered = _register_all()
    results: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registered": registered,
        "agent_probes": {},
        "persona_probes": {},
    }

    context = {"user_id": "U08K2UTG3G8", "channel": "D_TEST"}

    # Per-agent ping + help
    for agent, _mod in AGENTS:
        results["agent_probes"][agent] = {
            "ping": await _probe(f"{agent} ping", context),
            "help": await _probe(f"{agent} help", context),
        }

    # Persona alias round-trip (v0.7)
    for text_in, expected_target in PERSONA_CHECKS:
        results["persona_probes"][text_in] = {
            "expected_target": expected_target,
            "result": await _probe(text_in, context),
        }

    artifact_path = Path(__file__).parent / f"dispatch_roundtrip_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    artifact_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"artifact: {artifact_path}")
    print(f"registered: {', '.join(registered)}")

    # Short summary
    ok_count = 0
    fail_count = 0
    for agent, probes in results["agent_probes"].items():
        for kind, res in probes.items():
            if res["ok"] and not (isinstance(res.get("response"), dict) and res["response"].get("error")):
                ok_count += 1
            else:
                fail_count += 1
                print(f"  ✗ {agent} {kind}: {res}")

    for text_in, probe in results["persona_probes"].items():
        res = probe["result"]
        if res["ok"] and not (isinstance(res.get("response"), dict) and res["response"].get("error")):
            ok_count += 1
        else:
            fail_count += 1
            print(f"  ✗ persona '{text_in}': {res}")

    print(f"summary: {ok_count} ok, {fail_count} fail")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
