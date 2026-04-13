"""Weekly SF metadata snapshot writer.

Produces three deterministic markdown files under
`${REVOPS_REPO_ROOT}/var/knowledge_snapshots/<YYYY-MM-DD>/`:

- sf_object_model.md   — custom objects, custom fields, validation rules
- sf_automations.md    — active flows, apex triggers
- sf_users_roles.md    — users, roles, profiles

Files are sorted alphabetically and timestamps stripped so the markdown is
directly diffable week-over-week. Canonical versions (configured via
REVOPS_CANONICAL_KNOWLEDGE_DIR) are NEVER overwritten by this snapshotter —
only by `merger.merge()` after O explicit approval.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Callable

from shared.mcp import salesforce_mcp
from shared.secrets import get_config

log = logging.getLogger(__name__)

SNAPSHOT_FILES = ("sf_object_model.md", "sf_automations.md", "sf_users_roles.md")


def _snapshots_root() -> Path:
    root = get_config("REVOPS_REPO_ROOT") or str(Path(__file__).resolve().parents[3])
    return Path(root) / "var" / "knowledge_snapshots"


def _dir_for(target_date: str) -> Path:
    d = _snapshots_root() / target_date
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(fn: Callable[[], Any], label: str, default: Any) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — we want a partial snapshot on failures
        log.warning("snapshot step %s failed: %s", label, e)
        return default


def _render_object_model() -> str:
    """Custom objects + their custom fields + validation rules."""
    # Look up at call-time so monkeypatches on salesforce_mcp are honored.
    describe_org = salesforce_mcp._sf
    describe_sobject = salesforce_mcp.describe_sobject
    tooling_query = salesforce_mcp.tooling_query

    # `sf sobject list --sobject custom` returns a flat list[str] of API names;
    # we normalize to the same {name, label, custom} shape the renderer expects
    # by re-describing each custom object (label lives in the describe).
    raw = _safe(
        lambda: describe_org("sobject", "list", "--sobject", "custom"),
        "list_custom_sobjects",
        [],
    )
    names: list[str] = raw if isinstance(raw, list) else (raw.get("result") or [])
    custom_objects = sorted(
        ({"name": n, "label": n, "custom": True} for n in names if isinstance(n, str)),
        key=lambda s: s.get("name", ""),
    )

    md: list[str] = ["# Salesforce Object Model", ""]
    md.append(f"_Custom SObjects: {len(custom_objects)}_")
    md.append("")

    md.append("## Custom Objects Summary")
    md.append("")
    for s in custom_objects:
        md.append(f"- **{s.get('name','')}**")
    md.append("")

    for s in custom_objects:
        name = s.get("name", "")
        desc = _safe(lambda n=name: describe_sobject(n), f"describe:{name}", {})
        if not desc:
            continue
        md.append("---")
        md.append("")
        md.append(f"## {name}")
        md.append("")
        md.append(f"- **Label**: {desc.get('label','')}")
        md.append(f"- **Createable**: {desc.get('createable', False)}")
        md.append(f"- **Updateable**: {desc.get('updateable', False)}")
        md.append(f"- **Deletable**: {desc.get('deletable', False)}")
        md.append("")

        fields = sorted(
            [f for f in desc.get("fields", []) if f.get("custom")],
            key=lambda f: f.get("name", ""),
        )
        if fields:
            md.append(f"### Custom Fields ({len(fields)})")
            md.append("")
            md.append("| API Name | Label | Type | Required | Formula |")
            md.append("|----------|-------|------|----------|---------|")
            for f in fields:
                required = "Yes" if not f.get("nillable", True) and f.get("createable", True) else ""
                formula = "Yes" if f.get("calculatedFormula") else ""
                md.append(
                    f"| {f.get('name','')} | {f.get('label','')} | {f.get('type','')} "
                    f"| {required} | {formula} |"
                )
            md.append("")

        # SF rejects bulk SELECTs on Metadata when >1 row matches, so fetch
        # the summary columns in one query and pull Metadata per-rule.
        vr = _safe(
            lambda n=name: tooling_query(
                "SELECT Id,ValidationName,Active,Description,ErrorMessage "
                f"FROM ValidationRule WHERE EntityDefinition.QualifiedApiName='{n}' "
                "ORDER BY ValidationName"
            ),
            f"vr:{name}",
            {},
        )
        records = sorted(vr.get("records", []) or [], key=lambda r: r.get("ValidationName", ""))
        if records:
            md.append(f"### Validation Rules ({len(records)})")
            md.append("")
            for r in records:
                md.append(f"#### {r.get('ValidationName','')} ({'Active' if r.get('Active') else 'Inactive'})")
                md.append(f"- **Description**: {r.get('Description') or ''}")
                md.append(f"- **Error Message**: {r.get('ErrorMessage') or ''}")
                rule_id = r.get("Id")
                meta_rec = _safe(
                    lambda rid=rule_id: tooling_query(
                        f"SELECT Metadata FROM ValidationRule WHERE Id='{rid}'"
                    ),
                    f"vr-meta:{rule_id}",
                    {},
                )
                meta_rows = meta_rec.get("records", []) or []
                meta = (meta_rows[0].get("Metadata") if meta_rows else {}) or {}
                formula = meta.get("errorConditionFormula") if isinstance(meta, dict) else ""
                if formula:
                    md.append("- **Condition**:")
                    md.append("  ```")
                    md.append(f"  {formula}")
                    md.append("  ```")
                md.append("")

    return "\n".join(md).rstrip() + "\n"


def _render_automations() -> str:
    tooling_query = salesforce_mcp.tooling_query
    flows = _safe(
        lambda: tooling_query(
            "SELECT MasterLabel, Status, ProcessType, Description FROM Flow "
            "ORDER BY MasterLabel"
        ),
        "flows",
        {},
    )
    flow_rows = sorted(flows.get("records", []) or [], key=lambda r: r.get("MasterLabel", ""))

    triggers = _safe(
        lambda: tooling_query(
            "SELECT Name, TableEnumOrId, Status FROM ApexTrigger ORDER BY Name"
        ),
        "triggers",
        {},
    )
    trigger_rows = sorted(triggers.get("records", []) or [], key=lambda r: r.get("Name", ""))

    md = ["# Salesforce Automations", ""]
    md.append(f"## Flows ({len(flow_rows)})")
    md.append("")
    md.append("| Label | Status | Process Type |")
    md.append("|-------|--------|--------------|")
    for r in flow_rows:
        md.append(f"| {r.get('MasterLabel','')} | {r.get('Status','')} | {r.get('ProcessType','')} |")
    md.append("")
    md.append(f"## Apex Triggers ({len(trigger_rows)})")
    md.append("")
    md.append("| Name | Object | Status |")
    md.append("|------|--------|--------|")
    for r in trigger_rows:
        md.append(f"| {r.get('Name','')} | {r.get('TableEnumOrId','')} | {r.get('Status','')} |")
    md.append("")
    return "\n".join(md).rstrip() + "\n"


def _render_users_roles() -> str:
    soql_query = salesforce_mcp.soql_query
    users = _safe(
        lambda: soql_query(
            "SELECT Name, Username, Email, IsActive, UserRole.Name, Profile.Name "
            "FROM User WHERE IsActive = true ORDER BY Name",
            1000,
        ),
        "users",
        {},
    )
    roles = _safe(
        lambda: soql_query(
            "SELECT DeveloperName, Name, ParentRoleId FROM UserRole ORDER BY DeveloperName",
            500,
        ),
        "roles",
        {},
    )
    profiles = _safe(
        lambda: soql_query(
            "SELECT Name, UserType FROM Profile ORDER BY Name",
            500,
        ),
        "profiles",
        {},
    )

    user_rows = sorted(users.get("records", []) or [], key=lambda r: r.get("Name", ""))
    role_rows = sorted(roles.get("records", []) or [], key=lambda r: r.get("DeveloperName", ""))
    profile_rows = sorted(profiles.get("records", []) or [], key=lambda r: r.get("Name", ""))

    md = ["# Salesforce Users & Security", ""]
    md.append(f"## Active Users ({len(user_rows)})")
    md.append("")
    md.append("| Name | Username | Role | Profile |")
    md.append("|------|----------|------|---------|")
    for u in user_rows:
        role = (u.get("UserRole") or {}).get("Name", "") if isinstance(u.get("UserRole"), dict) else ""
        prof = (u.get("Profile") or {}).get("Name", "") if isinstance(u.get("Profile"), dict) else ""
        md.append(f"| {u.get('Name','')} | {u.get('Username','')} | {role} | {prof} |")
    md.append("")
    md.append(f"## Role Hierarchy ({len(role_rows)})")
    md.append("")
    md.append("| Developer Name | Label | Parent Role Id |")
    md.append("|---------------|-------|----------------|")
    for r in role_rows:
        md.append(f"| {r.get('DeveloperName','')} | {r.get('Name','')} | {r.get('ParentRoleId') or ''} |")
    md.append("")
    md.append(f"## Profiles ({len(profile_rows)})")
    md.append("")
    md.append("| Name | User Type |")
    md.append("|------|-----------|")
    for p in profile_rows:
        md.append(f"| {p.get('Name','')} | {p.get('UserType','')} |")
    md.append("")
    return "\n".join(md).rstrip() + "\n"


def snapshot(target_date: str | None = None) -> dict[str, Path]:
    """Run the three extractors and write markdown snapshots. Returns paths."""
    d = target_date or date.today().isoformat()
    out_dir = _dir_for(d)
    log.info("writing snapshot to %s", out_dir)

    outputs: dict[str, Path] = {}

    object_model = _render_object_model()
    p = out_dir / "sf_object_model.md"
    p.write_text(object_model, encoding="utf-8")
    outputs["object_model"] = p

    automations = _render_automations()
    p = out_dir / "sf_automations.md"
    p.write_text(automations, encoding="utf-8")
    outputs["automations"] = p

    users_roles = _render_users_roles()
    p = out_dir / "sf_users_roles.md"
    p.write_text(users_roles, encoding="utf-8")
    outputs["users_roles"] = p

    log.info("snapshot complete: %s", {k: str(v) for k, v in outputs.items()})
    return outputs


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Write SF metadata snapshot")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = snapshot(target_date=args.date)
    for k, v in paths.items():
        print(f"{k}: {v}")
