"""SF Lead Writer — schema-aware payload builder + approval-gated create.

Flow per lead:
  1. build_payload  — maps agent dict → SF Lead fields, with graceful fallback
                      to Description if custom fields aren't present yet.
  2. find_tlo_id    — SOQL lookup on Top_Level_Org__c by domain/parent-name.
  3. check_duplicate — Lead.Email + Contact.Email + Account.Website probe.
  4. create_lead    — dispatch through shared.mcp.salesforce_mcp.create_record,
                      which requires an approved approval_gate_id.

Schema probe:
  `describe_lead_custom_fields()` calls `describe_sobject("Lead")` once and
  caches the result. Agent custom fields we care about:
    - ICP_Score__c
    - Brand__c
    - Ownership_Type__c
    - Location_Count__c (optional — packed into Description if absent)

Description fallback format:
    [Loop ToF] ICP:82 | Tier:A | Brand:Arby's | Ownership:franchise_group | Locations:47

Intentionally conservative: we NEVER overwrite an existing Description —
the agent-written block is appended with a leading `[Loop ToF]` marker so
SDRs can spot + strip it after manual edits.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from shared.governance import write_audit

log = logging.getLogger(__name__)

# SF Lead custom fields the agent tries to populate. Order matters for the
# Description-fallback string.
_CUSTOM_FIELDS = (
    "ICP_Score__c",
    "ICP_Tier__c",
    "Brand__c",
    "Ownership_Type__c",
    "Location_Count__c",
)

_DESC_MARKER = "[Loop ToF]"
_AGENT_NAME = "top_of_funnel"


# ------------------------------------------------------- result dataclasses


@dataclass(frozen=True)
class DedupResult:
    is_duplicate: bool
    reason: str = ""
    existing_id: str | None = None  # Lead.Id or Contact.Id of the dupe
    existing_kind: str = ""         # 'lead' | 'contact' | 'account'


@dataclass(frozen=True)
class LeadPayload:
    fields: dict[str, Any]
    fallback_used: bool      # True if any custom field was packed into Description
    missing_fields: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- schema probe


def describe_lead_custom_fields(describe_fn: Callable[[str], dict[str, Any]] | None = None) -> set[str]:
    """Returns the subset of `_CUSTOM_FIELDS` that actually exist on Lead.

    `describe_fn` is injected in tests; production uses salesforce_mcp.describe_sobject.
    Soft-fails to an empty set on error — payload falls back to Description.
    """
    if describe_fn is None:
        from shared.mcp.salesforce_mcp import describe_sobject as _describe
        describe_fn = _describe

    try:
        meta = describe_fn("Lead") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("sf_lead_writer: describe_sobject failed — falling back to Description: %s", exc)
        return set()

    existing = {f.get("name") for f in (meta.get("fields") or [])}
    return {name for name in _CUSTOM_FIELDS if name in existing}


# --------------------------------------------------------------- TLO lookup


def find_tlo_id(
    *,
    domain: str | None,
    company_name: str | None,
    sf_query: Callable[[str], dict[str, Any]] | None = None,
) -> str | None:
    """Returns the Top_Level_Org__c.Id matching domain or parent name, or None."""
    if not domain and not company_name:
        return None

    if sf_query is None:
        from shared.mcp.salesforce_mcp import soql_query as _q
        sf_query = _q

    clauses = []
    if domain:
        safe = _soql_escape(domain.lower())
        clauses.append(f"Domain__c = '{safe}'")
    if company_name:
        safe = _soql_escape(company_name)
        clauses.append(f"Name = '{safe}'")
    where = " OR ".join(clauses)
    query = f"SELECT Id FROM Top_Level_Org__c WHERE {where} LIMIT 1"

    try:
        rows = (sf_query(query) or {}).get("records") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("sf_lead_writer: TLO lookup failed for %s / %s: %s", domain, company_name, exc)
        return None
    return rows[0].get("Id") if rows else None


# ----------------------------------------------------------------- dedup


def check_duplicate(
    *,
    email: str | None,
    domain: str | None,
    sf_query: Callable[[str], dict[str, Any]] | None = None,
) -> DedupResult:
    """Probe SF for a matching Lead, Contact, or Account. First hit wins.

    Match order:
      Lead.Email     → don't re-create a lead for an address SF already has.
      Contact.Email  → never lead-ify a known contact (would create a duplicate).
      Account.Website → an account at this domain already exists — flag so
                        sf_lead_writer logs dedup_skip but doesn't block (accounts
                        can have multiple legitimate leads).
    """
    if not email and not domain:
        return DedupResult(False, "no_identifiers")

    if sf_query is None:
        from shared.mcp.salesforce_mcp import soql_query as _q
        sf_query = _q

    try:
        if email:
            safe_email = _soql_escape(email)
            lead = sf_query(f"SELECT Id FROM Lead WHERE Email = '{safe_email}' LIMIT 1")
            lead_rows = (lead or {}).get("records") or []
            if lead_rows:
                return DedupResult(True, f"Lead.Email={email}", lead_rows[0].get("Id"), "lead")

            contact = sf_query(f"SELECT Id FROM Contact WHERE Email = '{safe_email}' LIMIT 1")
            contact_rows = (contact or {}).get("records") or []
            if contact_rows:
                return DedupResult(True, f"Contact.Email={email}", contact_rows[0].get("Id"), "contact")

        if domain:
            safe_domain = _soql_escape(domain.lower())
            acc = sf_query(
                f"SELECT Id FROM Account WHERE Website LIKE '%{safe_domain}%' LIMIT 1"
            )
            acc_rows = (acc or {}).get("records") or []
            if acc_rows:
                return DedupResult(
                    True, f"Account.Website~{domain}", acc_rows[0].get("Id"), "account"
                )

    except Exception as exc:  # noqa: BLE001
        # Fail-open: don't block pipeline on a dedup probe failure — the
        # create_record itself will still audit, and SF dedup rules are a
        # second backstop.
        log.warning("sf_lead_writer: dedup probe failed: %s", exc)
        return DedupResult(False, f"dedup_probe_failed: {exc}")

    return DedupResult(False, "no_match")


# -------------------------------------------------------------- payload build


def build_payload(
    lead: dict[str, Any],
    *,
    present_custom_fields: set[str],
    tlo_id: str | None = None,
    owner_id: str | None = None,
) -> LeadPayload:
    """Map agent lead dict → SF Lead field dict.

    Required fields (always present): FirstName, LastName, Email, Company.
    Custom fields (conditional): added if `present_custom_fields` contains them.
    Missing custom fields are packed into Description with a [Loop ToF] marker.
    """
    fields: dict[str, Any] = {
        "FirstName": (lead.get("first_name") or "").strip() or "Unknown",
        "LastName": (lead.get("last_name") or "").strip() or "Unknown",
        "Email": (lead.get("email") or "").strip(),
        "Company": (lead.get("company_name") or lead.get("domain") or "Unknown").strip(),
        "LeadSource": lead.get("lead_source") or "AI Prospecting — Top of Funnel",
    }
    if lead.get("phone"):
        fields["Phone"] = lead["phone"]
    if lead.get("title"):
        fields["Title"] = lead["title"]
    if lead.get("website") or lead.get("domain"):
        fields["Website"] = lead.get("website") or f"https://{lead['domain']}"
    if owner_id:
        fields["OwnerId"] = owner_id
    if tlo_id:
        fields["Top_Level_Org__c"] = tlo_id

    missing: list[str] = []
    description_kv: list[str] = []

    def put(custom_name: str, value: Any, desc_label: str) -> None:
        if value in (None, ""):
            return
        if custom_name in present_custom_fields:
            fields[custom_name] = value
        else:
            missing.append(custom_name)
            description_kv.append(f"{desc_label}:{value}")

    put("ICP_Score__c", lead.get("icp_score"), "ICP")
    put("ICP_Tier__c", lead.get("icp_tier"), "Tier")
    put("Brand__c", lead.get("brand"), "Brand")
    put("Ownership_Type__c", lead.get("ownership_type"), "Ownership")
    put("Location_Count__c", lead.get("location_count"), "Locations")

    if description_kv:
        fallback_line = f"{_DESC_MARKER} {' | '.join(description_kv)}"
        existing = (lead.get("description") or "").strip()
        fields["Description"] = f"{fallback_line}\n{existing}" if existing else fallback_line
    elif lead.get("description"):
        fields["Description"] = lead["description"]

    return LeadPayload(fields=fields, fallback_used=bool(description_kv), missing_fields=missing)


# --------------------------------------------------------------- create_lead


def create_lead(
    lead: dict[str, Any],
    *,
    approval_gate_id: int,
    describe_fn: Callable[[str], dict[str, Any]] | None = None,
    sf_query: Callable[[str], dict[str, Any]] | None = None,
    create_fn: Callable[..., dict[str, Any]] | None = None,
    skip_dedup: bool = False,
) -> dict[str, Any]:
    """Create one SF Lead. Returns {'sf_id', 'dedup', 'fallback_used'}.

    If a duplicate is detected, SKIPS the write and returns
    {'sf_id': None, 'dedup': DedupResult(...).__dict__, 'skipped': True}.
    """
    domain = (lead.get("domain") or "").lower() or None
    email = (lead.get("email") or "").strip().lower() or None

    if not skip_dedup:
        dup = check_duplicate(email=email, domain=domain, sf_query=sf_query)
        if dup.is_duplicate and dup.existing_kind in ("lead", "contact"):
            # Skip the gate reference — the gate gates writes, and this row
            # never got written. Avoids a FK violation when tests use a
            # synthetic gate_id.
            write_audit(
                agent_name=_AGENT_NAME,
                action="lead_create_dedup_skip",
                target=f"{dup.existing_kind}:{dup.existing_id}",
                after={"email": email, "domain": domain, "reason": dup.reason},
            )
            return {"sf_id": None, "dedup": dup.__dict__, "skipped": True, "fallback_used": False}

    present = describe_lead_custom_fields(describe_fn)
    tlo_id = find_tlo_id(domain=domain, company_name=lead.get("company_name"), sf_query=sf_query)
    payload = build_payload(
        lead,
        present_custom_fields=present,
        tlo_id=tlo_id,
        owner_id=lead.get("assigned_sdr_id"),
    )

    if create_fn is None:
        from shared.mcp.salesforce_mcp import create_record as _create
        create_fn = _create

    result = create_fn(
        "Lead",
        payload.fields,
        agent_name=_AGENT_NAME,
        approval_gate_id=approval_gate_id,
    )
    sf_id = result.get("id") or result.get("Id") or (result.get("result") or {}).get("id")
    return {
        "sf_id": sf_id,
        "dedup": None,
        "skipped": False,
        "fallback_used": payload.fallback_used,
        "missing_fields": payload.missing_fields,
        "tlo_id": tlo_id,
    }


# ----------------------------------------------------------------- helpers


def _soql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
