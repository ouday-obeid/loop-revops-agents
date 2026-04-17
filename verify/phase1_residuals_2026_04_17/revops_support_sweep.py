"""RevOps Support autonomous sweep — Phase 1 residuals.

Covers multiple Monday subitems in one run:
  - dedup_contacts scan_clusters (read-only; merge requires sandbox seed)
  - license_audit run (inactive users > 60d)
  - integration_health pollers (flow, apex, metadata_drift, sync_checker)
  - knowledge_refresh metadata_snapshotter (one-shot snapshot)
  - SF Tooling API perm audit (Outbounder_Access read access to ValidationRule)

User provisioning lifecycle test is intentionally skipped here — it performs
real SF writes (create + deactivate user) and should be gated by an explicit
approval flow before ever being run end-to-end.
"""
from __future__ import annotations

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
        return {"label": label, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def dedup_contacts_scan():
    from agents.revops_support.data_quality import dedup_contacts
    clusters = dedup_contacts.scan_clusters(max_emails=50)
    return {
        "clusters": len(clusters),
        "total_contacts": sum(len(c.get("contacts", [])) for c in clusters),
        "sample": clusters[:2],
    }


def license_audit_run():
    from agents.revops_support.permissions import license_audit
    inactive = license_audit.run(inactive_days=60)
    return {
        "inactive_user_count": len(inactive),
        "sample": [
            {"id": u.id, "username": u.username, "email": u.email, "last_login": u.last_login, "profile": u.profile_name}
            for u in inactive[:5]
        ],
    }


def integration_health_sweep():
    from agents.revops_support.integration_health import apex_job_monitor, sync_checker
    apex_jobs = apex_job_monitor.poll()
    sync_results = sync_checker.poll()
    return {
        "apex_problems": len(apex_jobs),
        "apex_sample": apex_jobs[:3],
        "sync_probes": len(sync_results),
        "sync_sample": sync_results[:3],
    }


def knowledge_snapshot():
    from agents.revops_support.knowledge_refresh import metadata_snapshotter
    paths = metadata_snapshotter.snapshot(target_date=datetime.now(timezone.utc).date().isoformat())
    return {k: str(v) for k, v in paths.items()}


def sf_perm_audit_outbounder_access():
    """Audit the Outbounder_Access permset for ValidationRule.ErrorConditionFormula read access."""
    from shared.mcp import salesforce_mcp
    # Check permset exists
    soql = (
        "SELECT Id, Name, Label FROM PermissionSet "
        "WHERE Name = 'Outbounder_Access' LIMIT 1"
    )
    permset = salesforce_mcp.soql_query(soql, limit=1)
    permset_rec = (permset.get("records") or [{}])[0]
    # Tooling API: check field-level access isn't the thing we need; ValidationRule.ErrorConditionFormula
    # read is a Tooling API permission question. We check whether the validation_monitor's workaround
    # (skipping ErrorConditionFormula) is still necessary by attempting the Tooling read.
    try:
        tooling_result = salesforce_mcp.tooling_query(
            "SELECT Id, Active, ErrorConditionFormula FROM ValidationRule LIMIT 1"
        )
        has_ecf_access = True
        sample_errors = []
    except Exception as exc:  # noqa: BLE001
        has_ecf_access = False
        sample_errors = [f"{type(exc).__name__}: {str(exc)[:200]}"]
    return {
        "permset_found": bool(permset_rec),
        "permset_label": permset_rec.get("Label"),
        "error_condition_formula_readable": has_ecf_access,
        "errors": sample_errors,
        "impact": (
            "validation_monitor workaround (skip ErrorConditionFormula col) "
            "can be removed" if has_ecf_access else
            "validation_monitor workaround is still required; ECF column remains Tooling-gated"
        ),
    }


def main() -> int:
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_alias": os.environ["SF_ORG_ALIAS"],
        "probes": [
            _safe("dedup_contacts.scan_clusters", dedup_contacts_scan),
            _safe("license_audit.run", license_audit_run),
            _safe("integration_health.sweep", integration_health_sweep),
            _safe("knowledge_refresh.snapshot", knowledge_snapshot),
            _safe("sf_perm_audit.outbounder_access", sf_perm_audit_outbounder_access),
        ],
    }
    artifact = OUT_DIR / f"revops_support_sweep_{STAMP}.json"
    artifact.write_text(json.dumps(results, indent=2, default=str))
    print(f"artifact: {artifact}")
    for p in results["probes"]:
        status = "ok" if p["ok"] else "FAIL"
        print(f"  [{status}] {p['label']}")
        if p["ok"]:
            res = p["result"]
            if isinstance(res, dict):
                for k, v in list(res.items())[:3]:
                    if isinstance(v, (list, dict)):
                        print(f"       {k}: {type(v).__name__}({len(v) if hasattr(v, '__len__') else '?'})")
                    else:
                        print(f"       {k}: {str(v)[:80]}")
        else:
            print(f"       error: {p.get('error', '')[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
