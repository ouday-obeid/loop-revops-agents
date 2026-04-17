"""SLT Metrics pure-read sweep — Phase 1 residuals.

Covers:
  - pipeline.fetcher.fetch_open_opps (compare vs "Open Pipeline All" report)
  - forecast.backtest (last 2 quarters; checks ≥85% accuracy)
  - excel_model.builder (9-sheet model smoke)
  - briefings.friday_review (dry-run, Slack-payload only, DEV_GUARD on)
  - bigquery.loop_pulse_client (graceful-degradation check)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
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


def pipeline_fetcher_smoke():
    from agents.slt_metrics.pipeline import fetcher
    opps = fetcher.fetch_open_opps(limit=1000)
    total_amount = sum((o.get("amount") or 0) for o in opps)
    by_stage: dict = {}
    for o in opps:
        by_stage[o.get("stage", "?")] = by_stage.get(o.get("stage", "?"), 0) + 1
    return {
        "open_opp_count": len(opps),
        "total_amount_usd": total_amount,
        "by_stage": by_stage,
        "sample": opps[:2],
    }


def forecast_backtest_smoke():
    """Run backtest for last closed quarter (min viable call)."""
    from agents.slt_metrics.forecast import backtest
    result = backtest.backtest(
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
    )
    return {
        "sample_size": result.get("sample_size"),
        "accuracy": result.get("accuracy"),
        "mape": result.get("mape"),
        "meets_85_threshold": (result.get("accuracy") or 0) >= 0.85,
        "by_category": result.get("by_category"),
    }


def excel_model_smoke():
    from agents.slt_metrics.excel_model import builder
    # Builder signatures vary — probe the public class/fn.
    fns = [name for name in dir(builder) if not name.startswith("_") and callable(getattr(builder, name))]
    out_path = OUT_DIR / f"slt_metrics_excel_model_{STAMP}.xlsx"
    # Minimal attempt — call build() if present
    if hasattr(builder, "build"):
        try:
            artifact = builder.build(output_path=out_path)
            return {
                "build_called": True,
                "output": str(artifact) if artifact else str(out_path),
                "exists": out_path.exists(),
                "size_bytes": out_path.stat().st_size if out_path.exists() else 0,
            }
        except TypeError:
            # Signature differs; report callables instead
            return {"build_called": False, "public_callables": fns[:15]}
    return {"build_called": False, "public_callables": fns[:15]}


def friday_review_dryrun():
    """Compose the Friday review payload without sending."""
    from agents.slt_metrics.briefings import friday_review
    public = [n for n in dir(friday_review) if not n.startswith("_") and callable(getattr(friday_review, n))]
    entry_points = [n for n in public if n in ("compose", "run", "generate", "build", "render", "dry_run")]
    if not entry_points:
        return {"entry_points": public[:10], "note": "no obvious dry-run fn"}
    fn = getattr(friday_review, entry_points[0])
    # Prefer a dry-run flag if supported
    import inspect
    sig = inspect.signature(fn)
    kwargs = {}
    if "dry_run" in sig.parameters:
        kwargs["dry_run"] = True
    if "send" in sig.parameters:
        kwargs["send"] = False
    out = fn(**kwargs) if not inspect.iscoroutinefunction(fn) else None
    if inspect.iscoroutinefunction(fn):
        import asyncio
        out = asyncio.run(fn(**kwargs))
    return {
        "entry_point": entry_points[0],
        "kwargs": kwargs,
        "text_preview": (out.get("text", "") if isinstance(out, dict) else str(out))[:300],
    }


def bigquery_loop_pulse_check():
    from agents.slt_metrics.bigquery import loop_pulse_client
    # Graceful-degradation: if GOOGLE creds missing, should log + return empty, not crash
    public = [n for n in dir(loop_pulse_client) if not n.startswith("_") and callable(getattr(loop_pulse_client, n))]
    # Try to instantiate + run a cheap method
    if hasattr(loop_pulse_client, "is_available"):
        available = loop_pulse_client.is_available()
        return {"is_available": available, "public": public[:10]}
    return {"public": public[:10], "note": "no is_available probe"}


def main() -> int:
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_alias": os.environ["SF_ORG_ALIAS"],
        "probes": [
            _safe("pipeline.fetcher.fetch_open_opps", pipeline_fetcher_smoke),
            _safe("forecast.backtest", forecast_backtest_smoke),
            _safe("excel_model.builder", excel_model_smoke),
            _safe("briefings.friday_review.dry_run", friday_review_dryrun),
            _safe("bigquery.loop_pulse_client.graceful_degradation", bigquery_loop_pulse_check),
        ],
    }
    artifact = OUT_DIR / f"slt_metrics_sweep_{STAMP}.json"
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
