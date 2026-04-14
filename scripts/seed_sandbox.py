#!/usr/bin/env python3
"""Seed the Loop AI `revagents` Salesforce sandbox with Agent-2-shaped test data.

What this creates (all tagged with `SEED-` prefix for easy cleanup):

  - 8 Accounts (restaurant-brand themed, matches Loop AI's vertical).
  - ~24 Contacts, 2-4 per Account, realistic emails so pre-demo brief's
    OpportunityContactRole email lookup returns hits.
  - 12 Opportunities covering every hygiene + deal-risk scenario:
      healthy, stale_activity, missing_next_step, past_close,
      single_threaded, pushed_close (via two-step update), amount_drop.
  - OpportunityContactRoles with primary contact flags.
  - Stale Tasks to force LastActivityDate back 30 days on the stale opps.
  - Synced Momentum-style Tasks (Type=Call, CallObject=SEED-MOM-...).
  - Recent Events with Subject containing "demo"/"discovery" for leaderboards.

Idempotent: re-running deletes everything named `SEED-%` first, then rebuilds
from scratch. Safe to run against the sandbox any number of times.

IMPORTANT: this only runs against the `revagents` sandbox alias. We guard
with a name check so it cannot accidentally point at prod.

Usage:
    python scripts/seed_sandbox.py [--purge-only]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

SF_ORG_ALIAS = "revagents"  # must match SF_SANDBOX_ORG_ALIAS in .env
SEED_PREFIX = "SEED-"

# -------------------------------------------------------------- sf CLI wrappers

def sf(*args: str, capture_json: bool = True) -> dict[str, Any]:
    """Run `sf <args> --json`, return parsed result dict. Stderr suppressed."""
    cmd = ["sf", *args]
    if capture_json and "--json" not in args:
        cmd.append("--json")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError(f"sf {' '.join(args)} failed: {proc.stderr[:400]}")
    try:
        return json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"sf returned non-JSON: {proc.stdout[:400]}") from e


def soql(query: str) -> list[dict[str, Any]]:
    out = sf("data", "query", "-o", SF_ORG_ALIAS, "-q", query)
    return out.get("result", {}).get("records", []) or []


def create_record(sobject: str, values: dict[str, Any]) -> str:
    """Return new record Id."""
    pairs = " ".join(f'{k}={_sf_value(v)}' for k, v in values.items())
    out = sf(
        "data", "create", "record",
        "-o", SF_ORG_ALIAS, "-s", sobject, "-v", pairs,
    )
    rec_id = out.get("result", {}).get("id")
    if not rec_id:
        raise RuntimeError(f"create {sobject} failed: {out}")
    return rec_id


def update_record(sobject: str, rec_id: str, values: dict[str, Any]) -> None:
    pairs = " ".join(f'{k}={_sf_value(v)}' for k, v in values.items())
    sf(
        "data", "update", "record",
        "-o", SF_ORG_ALIAS, "-s", sobject, "-i", rec_id, "-v", pairs,
    )


def delete_record(sobject: str, rec_id: str) -> None:
    sf(
        "data", "delete", "record",
        "-o", SF_ORG_ALIAS, "-s", sobject, "-i", rec_id,
    )


def _sf_value(v: Any) -> str:
    """Shell-quote values for sf CLI -v flag. Keep dates/strings readable."""
    if v is None:
        return '""'
    s = str(v)
    # sf CLI uses " " as pair separator — any space or quote needs escaping.
    if " " in s or '"' in s or "'" in s:
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s


# ---------------------------------------------------------------- sandbox guard

def assert_sandbox() -> None:
    out = sf("org", "display", "-o", SF_ORG_ALIAS)
    info = out.get("result", {})
    inst = (info.get("instanceUrl") or "").lower()
    username = info.get("username") or ""
    if "sandbox" not in inst:
        raise SystemExit(
            f"REFUSING — {SF_ORG_ALIAS} does not look like a sandbox "
            f"(instanceUrl={inst}, username={username})"
        )
    print(f"✓ sandbox confirmed: {username} @ {inst}")


# ------------------------------------------------------------- purge old seeds

def purge_seeds() -> None:
    """Delete every record whose Name starts with SEED-."""
    print("\n--- purging old seed records ---")
    # Delete in dependency order: Events, Tasks, OCRs, Opps, Contacts, Accounts.
    purges = [
        ("Event", f"SELECT Id FROM Event WHERE Subject LIKE '{SEED_PREFIX}%'"),
        ("Task", f"SELECT Id FROM Task WHERE Subject LIKE '{SEED_PREFIX}%'"),
        ("OpportunityContactRole",
         f"SELECT Id FROM OpportunityContactRole WHERE Opportunity.Name LIKE '{SEED_PREFIX}%'"),
        ("Opportunity", f"SELECT Id FROM Opportunity WHERE Name LIKE '{SEED_PREFIX}%'"),
        ("Contact", f"SELECT Id FROM Contact WHERE LastName LIKE '{SEED_PREFIX}%'"),
        ("Account", f"SELECT Id FROM Account WHERE Name LIKE '{SEED_PREFIX}%'"),
    ]
    for sobject, q in purges:
        rows = soql(q)
        if not rows:
            print(f"  {sobject}: 0 to delete")
            continue
        print(f"  {sobject}: deleting {len(rows)}")
        for r in rows:
            delete_record(sobject, r["Id"])


# ------------------------------------------------------------- build-time data

# Real Loop AI AEs + SDRs in sandbox. Fetched earlier via `sf data query`.
AE_EMAILS = [
    "alex@tryloop.ai.revagents",           # Alex Reyes
    "clay@tryloop.ai.revagents",           # Clayton Arvizu
    "dan.varela@tryloop.ai.revagents",     # Daniel Varela
    "jessy.calderon@tryloop.ai.revagents", # Jessy Calderon
    "nick.barbo@tryloop.ai.revagents",     # Nick Barbo
]
SDR_EMAILS = [
    "brad@tryloop.ai.revagents",        # Brad Dressler
    "peter@tryloop.ai.revagents",       # Peter Milillo
    "tyrell@tryloop.ai.revagents",      # Tyrell Belle
]

# Account + contact blueprints. Restaurant-brand flavored (Loop AI vertical).
ACCOUNTS = [
    {"name": f"{SEED_PREFIX}Acme Pizza",         "website": "acmepizza.example.com",  "domain": "acmepizza.example.com"},
    {"name": f"{SEED_PREFIX}Blue Plate Burgers", "website": "bluepb.example.com",     "domain": "bluepb.example.com"},
    {"name": f"{SEED_PREFIX}Crave Tacos",        "website": "crave-tacos.example.com","domain": "crave-tacos.example.com"},
    {"name": f"{SEED_PREFIX}Dragon Wok",         "website": "dragonwok.example.com",  "domain": "dragonwok.example.com"},
    {"name": f"{SEED_PREFIX}Evergreen Bakery",   "website": "evergreen.example.com",  "domain": "evergreen.example.com"},
    {"name": f"{SEED_PREFIX}Fjord Fish Co",      "website": "fjordfish.example.com",  "domain": "fjordfish.example.com"},
    {"name": f"{SEED_PREFIX}Grove Kitchen",      "website": "grovek.example.com",     "domain": "grovek.example.com"},
    {"name": f"{SEED_PREFIX}Horizon Grill",      "website": "horizon-grill.example.com","domain":"horizon-grill.example.com"},
]

# Opp templates: (account_idx, ae_idx, stage, amount, close_date_offset_days, has_next_step, scenario)
OPPS = [
    # --- healthy baseline -------------------------------------------------
    (0, 0, "Demo",     50000,  30, True,  "healthy_demo"),
    (1, 1, "Proposal", 120000, 45, True,  "healthy_proposal"),
    # --- stale_activity (LastActivityDate > 14d via old Tasks) ------------
    (2, 2, "Demo",     75000,  60, True,  "stale_demo"),
    (3, 3, "Proposal", 200000, 90, True,  "stale_proposal"),
    # --- missing_next_step (advanced stage, empty NextStep) ---------------
    (4, 4, "Proposal", 95000,  30, False, "missing_next_step_proposal"),
    (5, 0, "Demo",     60000,  20, False, "missing_next_step_demo"),
    # --- past_close (CloseDate in past, still open) -----------------------
    (6, 1, "Demo",     40000, -10, True,  "past_close_demo"),
    (7, 2, "Proposal", 150000, -5, True,  "past_close_proposal"),
    # --- single_threaded (advanced stage, exactly 1 OCR) ------------------
    (0, 3, "Proposal", 80000,  35, True,  "single_threaded_proposal"),
    # --- pushed_close (updated post-create to move CloseDate forward) -----
    (1, 4, "Demo",     90000,  15, True,  "pushed_close_demo"),
    # --- amount_drop (updated post-create to reduce Amount) ---------------
    (2, 0, "Proposal", 200000, 40, True,  "amount_drop_proposal"),
    # --- competitor_mention: no Fireflies in sandbox — skip ---------------
    # --- healthy "recent demo won" for leaderboard signal -----------------
    (3, 1, "Demo",     110000, 25, True,  "healthy_demo_2"),
]


# --------------------------------------------------------- contact generator

def contact_blueprints(account: dict[str, Any], account_idx: int) -> list[dict[str, Any]]:
    """3 contacts per account: a primary (Decision Maker), a secondary, a champion."""
    domain = account["domain"]
    base = account["name"].replace(SEED_PREFIX, "").replace(" ", "").lower()[:10]
    return [
        {"FirstName": "Pat",   "LastName": f"{SEED_PREFIX}DM{account_idx}",       "Email": f"pat.dm@{domain}",       "Title": "VP Operations"},
        {"FirstName": "Jordan","LastName": f"{SEED_PREFIX}Buyer{account_idx}",    "Email": f"jordan.buyer@{domain}", "Title": "CFO"},
        {"FirstName": "Morgan","LastName": f"{SEED_PREFIX}Champ{account_idx}",    "Email": f"morgan.champ@{domain}", "Title": "Director of Delivery"},
    ]


# --------------------------------------------------------- user lookup cache

def user_map(emails: list[str]) -> dict[str, str]:
    """Email → Id for the given list of usernames."""
    clause = "(" + ",".join(f"'{e}'" for e in emails) + ")"
    rows = soql(f"SELECT Id, Username FROM User WHERE Username IN {clause}")
    return {r["Username"]: r["Id"] for r in rows}


# --------------------------------------------------------- main seed routine

def seed() -> dict[str, Any]:
    print("\n--- creating accounts ---")
    acct_ids: list[str] = []
    for a in ACCOUNTS:
        rid = create_record("Account", {"Name": a["name"], "Website": a["website"]})
        acct_ids.append(rid)
        print(f"  Account {a['name']} -> {rid}")

    print("\n--- creating contacts ---")
    contact_ids: list[list[str]] = []  # per-account list of contact ids
    contact_emails: list[list[str]] = []
    for idx, (acct_id, acct) in enumerate(zip(acct_ids, ACCOUNTS)):
        per_acct_ids: list[str] = []
        per_acct_emails: list[str] = []
        for c in contact_blueprints(acct, idx):
            rid = create_record("Contact", {
                "FirstName": c["FirstName"],
                "LastName": c["LastName"],
                "Email": c["Email"],
                "Title": c["Title"],
                "AccountId": acct_id,
            })
            per_acct_ids.append(rid)
            per_acct_emails.append(c["Email"])
        contact_ids.append(per_acct_ids)
        contact_emails.append(per_acct_emails)
        print(f"  Account #{idx}: {len(per_acct_ids)} contacts")

    print("\n--- resolving user IDs ---")
    users = user_map(AE_EMAILS + SDR_EMAILS)
    missing = [e for e in AE_EMAILS + SDR_EMAILS if e not in users]
    if missing:
        print(f"  ⚠️  missing users in sandbox (skipping as owners): {missing}")
    ae_ids = [users[e] for e in AE_EMAILS if e in users]
    sdr_ids = [users[e] for e in SDR_EMAILS if e in users]
    print(f"  {len(ae_ids)} AE owner IDs, {len(sdr_ids)} SDR owner IDs resolved")
    if not ae_ids:
        raise SystemExit("no AE users resolved — check email list")

    print("\n--- creating opportunities ---")
    today = date.today()
    opp_records: list[dict[str, Any]] = []
    for (acct_idx, ae_idx, stage, amount, close_offset, has_next_step, scenario) in OPPS:
        ae_id = ae_ids[ae_idx % len(ae_ids)]
        close_date = (today + timedelta(days=close_offset)).isoformat()
        vals = {
            "Name": f"{SEED_PREFIX}{ACCOUNTS[acct_idx]['name'].replace(SEED_PREFIX,'')} - {scenario}",
            "StageName": stage,
            "CloseDate": close_date,
            "Amount": amount,
            "AccountId": acct_ids[acct_idx],
            "OwnerId": ae_id,
        }
        # Loop AI's org replaces standard NextStep with Next_Steps__c.
        # pipeline_hygiene.py queries `NextStep` — this mismatch means
        # missing_next_step will always fire against real Loop data until
        # the agent code is updated. Populating both for forward compat.
        if has_next_step:
            vals["Next_Steps__c"] = f"Follow-up on {scenario}"
        rid = create_record("Opportunity", vals)
        opp_records.append({
            "id": rid,
            "account_idx": acct_idx,
            "scenario": scenario,
            "stage": stage,
            "amount": amount,
            "close_date": close_date,
            "ae_id": ae_id,
        })
        print(f"  Opp {scenario} -> {rid}")

    print("\n--- creating contact roles ---")
    for opp in opp_records:
        acct_idx = opp["account_idx"]
        all_contact_ids = contact_ids[acct_idx]
        # single_threaded scenario: one OCR only. Everything else: 2-3 OCRs.
        if opp["scenario"].startswith("single_threaded"):
            chosen = all_contact_ids[:1]
        else:
            chosen = all_contact_ids[:3]
        for i, cid in enumerate(chosen):
            create_record("OpportunityContactRole", {
                "OpportunityId": opp["id"],
                "ContactId": cid,
                "Role": "Decision Maker" if i == 0 else "Evaluator",
                "IsPrimary": "true" if i == 0 else "false",
            })
        print(f"  OCRs on {opp['scenario']}: {len(chosen)}")

    print("\n--- stamping stale Tasks (LastActivityDate back-date) ---")
    # LastActivityDate on Opp is derived from Task.ActivityDate / Event.ActivityDate
    # rolled up to WhatId. Create Tasks with an old ActivityDate against the stale opps.
    stale_opps = [o for o in opp_records if o["scenario"].startswith("stale")]
    old_date = (today - timedelta(days=30)).isoformat()
    for opp in stale_opps:
        create_record("Task", {
            "Subject": f"{SEED_PREFIX}old followup",
            "Status": "Completed",
            "Priority": "Normal",
            "ActivityDate": old_date,
            "WhatId": opp["id"],
            "OwnerId": opp["ae_id"],
        })
    # Healthy opps get a recent Task so LastActivityDate is inside the window.
    recent_opps = [o for o in opp_records if o["scenario"].startswith("healthy")]
    recent_date = today.isoformat()
    for opp in recent_opps:
        create_record("Task", {
            "Subject": f"{SEED_PREFIX}recent touchpoint",
            "Status": "Completed",
            "Priority": "Normal",
            "ActivityDate": recent_date,
            "WhatId": opp["id"],
            "OwnerId": opp["ae_id"],
        })
    print(f"  stale opps: {len(stale_opps)} old Tasks, healthy opps: {len(recent_opps)} recent Tasks")

    print("\n--- synthetic pushed_close + amount_drop via update (triggers FieldHistory) ---")
    pushed = next((o for o in opp_records if o["scenario"] == "pushed_close_demo"), None)
    if pushed:
        new_close = (today + timedelta(days=40)).isoformat()  # +25d forward from original +15
        time.sleep(1)  # ensure CreatedDate on history row is after opp CreatedDate
        update_record("Opportunity", pushed["id"], {"CloseDate": new_close})
        print(f"  pushed_close: {pushed['id']} moved CloseDate to {new_close}")

    dropped = next((o for o in opp_records if o["scenario"] == "amount_drop_proposal"), None)
    if dropped:
        time.sleep(1)
        update_record("Opportunity", dropped["id"], {"Amount": 90000})  # 200k → 90k = 55% drop
        print(f"  amount_drop: {dropped['id']} Amount dropped to 90000")

    print("\n--- Momentum-synced Call Tasks (Type=Call + CallObject) ---")
    # Two "synced" calls — these should NOT flag as sync breaks if Momentum API
    # reports matching call IDs. Verifying this requires a live Momentum mock.
    synced_calls = [
        {"call_id": f"{SEED_PREFIX}MOM-001", "rep_email": AE_EMAILS[0], "contact_email": contact_emails[0][0]},
        {"call_id": f"{SEED_PREFIX}MOM-002", "rep_email": AE_EMAILS[1], "contact_email": contact_emails[1][0]},
    ]
    for call in synced_calls:
        owner_id = users.get(call["rep_email"])
        if not owner_id:
            continue
        # Loop AI's org has removed the standard Task.Type field.
        # momentum_sync_monitor's fallback time-window probe uses Type='Call'
        # and will silently return no matches in prod. Primary CallObject
        # probe still works — which is the match path this seed targets.
        create_record("Task", {
            "Subject": f"{SEED_PREFIX}Call with {call['contact_email']}",
            "Status": "Completed",
            "ActivityDate": today.isoformat(),
            "CallObject": call["call_id"],
            "OwnerId": owner_id,
        })
    print(f"  {len(synced_calls)} synced Call Tasks created")

    print("\n--- SDR demo/discovery Events (for leaderboards) ---")
    # Subject contains 'demo' or 'discovery', owned by SDRs.
    if sdr_ids:
        for i, sdr_id in enumerate(sdr_ids):
            start = datetime.now(timezone.utc) - timedelta(days=i + 1)
            end = start + timedelta(minutes=30)
            create_record("Event", {
                "Subject": f"{SEED_PREFIX}Intro demo - {ACCOUNTS[i % len(ACCOUNTS)]['name'].replace(SEED_PREFIX,'')}",
                "StartDateTime": start.isoformat(timespec="seconds"),
                "EndDateTime": end.isoformat(timespec="seconds"),
                "OwnerId": sdr_id,
            })
            discovery_start = datetime.now(timezone.utc) - timedelta(days=i + 2)
            create_record("Event", {
                "Subject": f"{SEED_PREFIX}Discovery call - {ACCOUNTS[(i+1) % len(ACCOUNTS)]['name'].replace(SEED_PREFIX,'')}",
                "StartDateTime": discovery_start.isoformat(timespec="seconds"),
                "EndDateTime": (discovery_start + timedelta(minutes=30)).isoformat(timespec="seconds"),
                "OwnerId": sdr_id,
            })
    print(f"  {2 * len(sdr_ids)} SDR Events created (demo + discovery)")

    return {
        "accounts": len(acct_ids),
        "contacts": sum(len(c) for c in contact_ids),
        "opportunities": len(opp_records),
        "scenarios": [o["scenario"] for o in opp_records],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge-only", action="store_true",
                    help="delete all SEED- records and exit, no rebuild")
    args = ap.parse_args()

    assert_sandbox()
    purge_seeds()
    if args.purge_only:
        print("\n✓ purge-only complete")
        return 0

    summary = seed()
    print("\n--- done ---")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
