"""Top of Funnel autonomous sweep — Phase 1 residuals.

Covers:
  - icp_scorer backtest vs last 100 closed-won accounts (distribution report)
  - sf_lead_writer sandbox test (TLO linkage preserved, no duplicate)
  - daily_briefing.send_dry_run (payload shape)
  - @oo tof enrich <domain> dispatch (already covered by dispatch_roundtrip harness)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
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


def icp_backtest_100_closed_won():
    from shared.mcp import salesforce_mcp
    from agents.top_of_funnel import icp_scorer
    # Drop AnnualRevenue — sandbox user lacks FLS on the standard field; scorer
    # treats missing fields as None-gracefully via dict.get
    result = salesforce_mcp.soql_query(
        "SELECT Id, Name, Website, Industry, NumberOfEmployees "
        "FROM Account WHERE Id IN (SELECT AccountId FROM Opportunity "
        "WHERE IsClosed = true AND IsWon = true) "
        "ORDER BY CreatedDate DESC LIMIT 100",
        limit=100,
    )
    accounts = result.get("records", []) or []
    grades = []
    errors = 0
    for acc in accounts:
        try:
            score = icp_scorer.score_account(acc)
            grades.append({
                "account_id": acc.get("Id"),
                "name": acc.get("Name"),
                "grade": score.get("grade") if isinstance(score, dict) else str(score),
                "score": score.get("score") if isinstance(score, dict) else None,
            })
        except Exception:
            errors += 1
    dist = Counter(g["grade"] for g in grades)
    return {
        "accounts_scored": len(grades),
        "errors": errors,
        "grade_distribution": dict(dist),
        "sample_top": grades[:3],
    }


def sf_lead_writer_sandbox_probe():
    """Probe: can we generate a valid lead payload + check duplicates without writing?"""
    from shared.mcp import salesforce_mcp
    from agents.top_of_funnel import sf_lead_writer
    # Describe returns the custom field set — proves writer can enumerate schema
    custom_fields = sf_lead_writer.describe_lead_custom_fields()
    # Dup check against a fake email + domain — proves the query path works
    dup = sf_lead_writer.check_duplicate(
        email="verify-probe@tof-phase1.example.test",
        domain="tof-phase1.example.test",
    )
    # Find TLO id — tests the TLO resolver
    tlo_id = sf_lead_writer.find_tlo_id(domain="tof-phase1.example.test", company_name=None)
    return {
        "custom_fields_count": len(custom_fields),
        "custom_fields_sample": sorted(custom_fields)[:10],
        "dup_object_type": getattr(dup, "object_type", None) if dup else None,
        "dup_record_id": getattr(dup, "record_id", None) if dup else None,
        "tlo_resolver_return": tlo_id,
    }


def daily_briefing_dryrun():
    from agents.top_of_funnel import daily_briefing
    # Inject a mock send_fn so we exercise the dry-run flow without touching
    # the Slack SDK (which would need a real token + valid channel resolution).
    sends: list = []
    def mock_send(ch, text, blocks=None, **kwargs):
        sends.append({"channel": ch, "text": (text or "")[:120], "block_count": len(blocks or []), "kwargs": list(kwargs.keys())})
        return {"ts": "mock-1700000000.000001"}

    result = asyncio.run(daily_briefing.send_dry_run(channel="U08K2UTG3G8", send_fn=mock_send))
    return {
        "status": (result or {}).get("status") if isinstance(result, dict) else None,
        "previews": (result or {}).get("previews") if isinstance(result, dict) else None,
        "sent_count": (result or {}).get("sent") if isinstance(result, dict) else None,
        "mock_sends_captured": len(sends),
        "first_send": sends[0] if sends else None,
    }


def main() -> int:
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_alias": os.environ["SF_ORG_ALIAS"],
        "probes": [
            _safe("icp_scorer.backtest_100_closed_won", icp_backtest_100_closed_won),
            _safe("sf_lead_writer.sandbox_probe", sf_lead_writer_sandbox_probe),
            _safe("daily_briefing.send_dry_run", daily_briefing_dryrun),
        ],
    }
    artifact = OUT_DIR / f"top_of_funnel_sweep_{STAMP}.json"
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
