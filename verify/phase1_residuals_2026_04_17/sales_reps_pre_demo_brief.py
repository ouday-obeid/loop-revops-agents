"""Sales Reps pre-demo brief dry-run — Phase 1 residual.

Generates a pre-demo brief against a sandbox SEED opportunity. Fireflies and Clay
calls will gracefully degrade (empty results) because FIREFLIES_API_KEY + CLAY_API_KEY
are placeholders; the brief_generator is designed to handle that and still produce
SF-sourced structure (opp, account, attendees, talking_points, gaps).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["SF_ORG_ALIAS"] = "revagents"
os.environ.setdefault("SLACK_DEV_GUARD", "1")
os.environ.setdefault("SLACK_TEST_CHANNEL", "U08K2UTG3G8")

# SEED opp surfaced by pipeline_hygiene sandbox run (2026-04-17)
TEST_OPP_ID = "006WB00000JcWn7YAF"  # SEED-Crave Tacos - stale_demo


async def main() -> int:
    from agents.sales_reps.pre_demo import brief_generator

    result = await brief_generator.generate(TEST_OPP_ID, include_blocks=False)
    artifact = Path(__file__).parent / f"sales_reps_pre_demo_brief_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    artifact.write_text(json.dumps(result, indent=2, default=str))
    print(f"artifact: {artifact}")
    print(f"target: {TEST_OPP_ID}")
    if "error" in result:
        print(f"  error: {result['error']} — {result.get('text', '')[:120]}")
        return 0  # partial degradation is expected under REPLACE secrets
    print(f"  opp: {result.get('opportunity_name')} / {result.get('account_name')}")
    print(f"  stage: {result.get('stage')} · amount: {result.get('amount')} · close: {result.get('close_date')}")
    print(f"  people: {len(result.get('people', []))} · prior_calls: {len(result.get('prior_calls', []))} · news: {len(result.get('news', []))}")
    print(f"  talking_points: {len(result.get('talking_points', []))} · gaps: {len(result.get('gaps', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
