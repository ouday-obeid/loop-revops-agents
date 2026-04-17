"""CS autonomous sweep — Phase 1 residuals.

Covers:
  - uid_resolver.resolve on synthetic Vitally payloads (unit-level, no API needed)
  - expansion_detector.run_sweep (SF + Fireflies; Fireflies will degrade)
  - renewal.pipeline.run_sweep (dry_run=True, SF-only)
  - reports.weekly.build_report (local DB read)
  - vitally integration: attempt instantiation; gracefully degrade if VITALLY_API_KEY missing

Full Vitally UID match-rate scan requires a live VITALLY_API_KEY. We report
whether the key is live, so the subitem can reflect partial vs full verification.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["SF_ORG_ALIAS"] = "revagents"
os.environ["SF_SANDBOX_ORG_ALIAS"] = "revagents"
os.environ.setdefault("SLACK_DEV_GUARD", "1")

OUT_DIR = Path(__file__).parent
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe(label, fn):
    try:
        return {"label": label, "ok": True, "result": fn()}
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def uid_resolver_synthetic():
    """Validate resolve() returns externalId when present, None + log_miss otherwise."""
    from agents.cs.health import uid_resolver
    good = {"id": "v-123", "externalId": "001WB000010OIOTYA4", "name": "Grove Kitchen"}
    bad = {"id": "v-456", "name": "Missing ExternalId Co"}
    uid_a = uid_resolver.resolve(good)
    uid_b = uid_resolver.resolve(bad)
    return {
        "with_externalId": uid_a,
        "without_externalId": uid_b,
        "behavior_ok": uid_a == "001WB000010OIOTYA4" and uid_b is None,
    }


def expansion_run_sweep():
    return asyncio.run(_expansion_run())


async def _expansion_run():
    from agents.cs.expansion import expansion_detector
    counters = await expansion_detector.run_sweep()
    return dict(counters)


def renewal_pipeline_dryrun():
    return asyncio.run(_renewal_dryrun())


async def _renewal_dryrun():
    from agents.cs.renewal import pipeline
    counters = await pipeline.run_sweep(dry_run=True)
    return dict(counters)


def weekly_report_build():
    from agents.cs.reports import weekly
    text = weekly.build_report()
    return {
        "length_chars": len(text),
        "preview": text[:500],
    }


def vitally_key_status():
    """Report whether VITALLY_API_KEY is live (not REPLACE) — gates the full UID match run."""
    from shared.secrets import get_config
    val = get_config("VITALLY_API_KEY") or ""
    live = bool(val) and val != "REPLACE"
    return {
        "vitally_api_key_set": live,
        "note": "UID match-rate scan requires live key" if not live else "key live — full scan can be triggered separately",
    }


def main() -> int:
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_alias": os.environ["SF_ORG_ALIAS"],
        "probes": [
            _safe("uid_resolver.synthetic", uid_resolver_synthetic),
            _safe("expansion_detector.run_sweep", expansion_run_sweep),
            _safe("renewal.pipeline.run_sweep_dryrun", renewal_pipeline_dryrun),
            _safe("reports.weekly.build_report", weekly_report_build),
            _safe("vitally.key_status", vitally_key_status),
        ],
    }
    artifact = OUT_DIR / f"cs_sweep_{STAMP}.json"
    artifact.write_text(json.dumps(results, indent=2, default=str))
    print(f"artifact: {artifact}")
    for p in results["probes"]:
        status = "ok" if p["ok"] else "FAIL"
        print(f"  [{status}] {p['label']}")
        if p["ok"]:
            res = p["result"]
            if isinstance(res, dict):
                for k, v in list(res.items())[:4]:
                    if isinstance(v, (list, dict)):
                        print(f"       {k}: {type(v).__name__}({len(v) if hasattr(v, '__len__') else '?'})")
                    else:
                        print(f"       {k}: {str(v)[:80]}")
        else:
            print(f"       error: {p.get('error', '')[:180]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
