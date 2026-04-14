# Phase 0 amendment — code review

**Reviewer:** Agent 1 (Top of Funnel)  /  **Date:** 2026-04-13
**PR doc:** `_PHASE_0_AMENDMENT_PR.md`
**Overall verdict:** APPROVE with three change requests and one architectural question.

> **Status (2026-04-14):** LANDED + cleanup applied. All diffs are in `main` via the Phase 1 specialist merges. Change requests (b) drop dead env vars and (c) strike Diff 7 are now applied on branch `tof/amendment-memo-cleanup`. Change request (a) — `shared.config.loader` scope decision — remains open and is deferred to O. See the `Actionable punch list before merge` section for the current state of each item.

Scope audit passes — grep confirms no edits to `agent_base.py`, `salesforce_mcp.py`, `slack_dispatcher.py`, or any existing `APPROVAL_TIERS` / `RATE_LIMITS` entry. Every diff is a new row, new file, or new helper. Merge risk is low; none of the eight diffs block D1–D10 (TOF already ships with local workarounds that become one-line re-exports post-merge).

---

## Per-diff findings

### Diff 1 — `shared/runtime/schedule.py` → APPROVE

Two `Job` rows for enrichment pipeline + daily briefing. Verified:
- Callable paths resolve to real functions (`enrichment.pipeline:run_pipeline` and `daily_briefing:send_daily_briefing` both exist; hit in `test_end_to_end.py`).
- Cron expressions valid (`0 2 * * 1-5`, `55 7 * * 1-5`).
- Matches existing Phase 1 blocks (onboarding-*, cs-*, revops-support-*, slt-*).

No changes requested.

### Diff 2 — `shared/governance.py APPROVAL_TIERS` → APPROVE (flag: unused at merge)

`suppression_override` with `slack_button` gate, `o_or_dept_head` approver, `requires_justification=True`. Pattern is identical to `csm_reassignment` / `mark_churned_request` — clean.

Caveat: grep shows zero TOF production code calling `create_approval_gate(action_type="suppression_override", ...)` today (only referenced in `SKILL.md` and the PR doc itself). The tier is groundwork for a future SDR Slack override flow, not an in-use gate. Fine to merge ahead of the consumer; O should know this is scaffolding.

### Diff 3–4 — `shared/config/__init__.py` + `loader.py` → APPROVE-WITH-CONCERN

Loader is clean (lru_cache keyed on path + mtime_ns, safe_load only). Behaviorally identical to the inline `icp_scorer.py:_load_yaml_cached` already in TOF.

**Concern — single consumer / premature abstraction.** Only `agents/top_of_funnel/suppression.py` would call it. Elsewhere:
- `agents/top_of_funnel/icp_scorer.py` has its own inline `_load_yaml_cached`.
- `agents/top_of_funnel/routing.py` calls `yaml.safe_load` directly.
- `agents/revops_support/schema/*.py` has **8 direct `yaml.safe_load` calls** across 6 files.

If this is meant to be the Phase 1 standard, the PR should either (a) migrate the 10+ existing call sites, or (b) inline in TOF and drop `shared/config/loader.py`. Current shape — "new shared helper, one caller" — violates the "three similar lines beats premature abstraction" rule.

**Recommendation:** pick a lane. Either inline in TOF, or merge as proposed AND file a follow-up to sweep `icp_scorer._load_yaml_cached` + all `revops_support.schema.*` call sites onto `load_yaml`.

### Diff 5 — `shared/config/suppression_extras.yaml` → APPROVE

Seed content matches `agents/top_of_funnel/config/suppression_local.yaml` (which carries a self-destruct comment "becomes redundant once Phase 0 amendment lands"). `suppression.py` already prefers `shared/config/` and falls back to `config/suppression_local.yaml`; landing this file silently flips the active source.

Safe-cutover is explicitly tested: `test_competitor_domain_loader_uses_shared_first` (`tests/test_suppression.py:302`) asserts shared wins when both exist.

**Follow-up (out of scope for this PR):** delete `agents/top_of_funnel/config/suppression_local.yaml` once this merges — its own header says so.

### Diff 6 — `shared/db/connection.py get_agent_engine` → APPROVE

Pattern mirrors the existing `get_engine()` (lru_cache, sqlite `connect_args`, `PRAGMA foreign_keys=ON`). `maxsize=16` is ample for the six Phase 1 specialists.

Functional equivalence verified against the already-shipping `agents/top_of_funnel/state.py:get_state_engine`. When this merges, `state.py` collapses to one line:

```python
from shared.db.connection import get_agent_engine
def get_state_engine(): return get_agent_engine("top_of_funnel")
```

(Clear in the shipping file's header comment; the shim was deliberately structured for zero-churn replacement.)

### Diff 7 — `pyproject.toml testpaths` → ALREADY APPLIED

Current `pyproject.toml` already lists `agents/top_of_funnel/tests` plus six other Phase 1 agent paths. This diff is historical. Strike from the PR or mark it done.

### Diff 8 — `.env.example` additions → CHANGE REQUESTED

Line-by-line:

| Var | Verdict | Evidence |
|---|---|---|
| `CLAY_MONTHLY_BUDGET_CREDITS=50000` | **KEEP** | Consumed in `enrichment/clay_client.py:87` |
| `NOOKS_CADENCE_SF_OBJECT=CampaignMember` | **KEEP** | Consumed in `sequence_enroller.py:78` |
| `TOF_SDR_SLACK_MAP_JSON={}` | **DROP** | Only referenced at `tests/conftest.py:41` as a `setdefault`. No production consumer. Slack IDs come from `territory.yaml` rotation entries (`routing.py:load_territory`). Dead env var. |
| `AGENT_SF_USER_TOF=tof-agent@tryloop.ai` | **DROP or wire** | Only at `tests/conftest.py:42`. Phase 0 uses the single `SF_WRITE_ORG_ALIAS`; per-agent service-user aliases aren't the current architecture. Either wire into `_resolve_org_alias` with explicit per-agent fallback logic (and document) or drop. |

---

## Test plan — notes

Steps 1–4 are correct post-merge. Add one more as a smoke on Diff 6:

```bash
python -c "from shared.db.connection import get_agent_engine; \
  e = get_agent_engine('top_of_funnel'); assert 'top_of_funnel/state.db' in str(e.url), e.url"
```

And one cutover assertion for Diff 5:

```bash
python -c "from agents.top_of_funnel.suppression import _load_competitor_domains; \
  d = _load_competitor_domains(); assert 'olo.com' in d and 'otter.ai' in d, d"
```

(`otter.ai` is only in the shared-config version; its presence proves the shared path is active.)

---

## Actionable punch list before merge

1. **Decide** `shared.config.loader` scope — TOF-only inline vs full Phase 1 sweep. If sweep, file follow-up to migrate `icp_scorer` + `revops_support.schema.*`. *(OPEN — deferred to O)*
2. ~~**Drop** `TOF_SDR_SLACK_MAP_JSON` and `AGENT_SF_USER_TOF` from Diff 8 (or wire them).~~ *(DONE 2026-04-14 on `tof/amendment-memo-cleanup` — setdefault lines removed from `tests/conftest.py`; Diff 8 memo cleaned.)*
3. ~~**Remove** Diff 7 from the PR (already merged upstream).~~ *(DONE 2026-04-14 on `tof/amendment-memo-cleanup` — section struck from `_PHASE_0_AMENDMENT_PR.md`.)*
4. ~~**Add** the two test-plan smokes above.~~ *(DONE 2026-04-14 on `tof/amendment-memo-cleanup` — `tests/test_db_connection.py` + `test_suppression_extras_yaml_has_known_competitors` added; regression coverage table in `_PHASE_0_AMENDMENT_PR.md`.)*

## Follow-ups spawned by merge (not in-scope for this PR)

- Delete `agents/top_of_funnel/config/suppression_local.yaml`.
- Reduce `agents/top_of_funnel/state.py` to one-line re-export of `get_agent_engine`.
- Wire a consumer for `suppression_override` tier (SDR Slack override flow).

## Risk summary

**Low.** Verified zero Phase 0 public-surface edits, zero regressions in the current 221-test ToF suite, and every proposed addition has a downstream consumer or a well-structured shim waiting to re-export it. The only real review friction is Diff 3–4's premature-abstraction question and Diff 8's dead env vars.
