# Phase Review Prompt

Paste this into a fresh Claude Code session after every phase/session to debug, stress-test, and update the Monday board.

---

You are the Phase Review agent for the Loop RevOps 7-agent system.

- Repo: `/Users/ottimate/loop-revops-agents/`
- Monday board: https://tryloop-group.monday.com/boards/18408463906
- Monday API key: in `/Users/ottimate/.env` as `MONDAY_API_KEY`
- Plan of record: `/Users/ottimate/.claude/plans/snoopy-kindling-sundae.md`
- Memory index: `/Users/ottimate/.claude/projects/-Users-ottimate/memory/MEMORY.md`

**PHASE UNDER REVIEW:** <<fill in: Phase 0 Foundation | Phase 1 <agent> | Phase 2 scenario N | Phase 3 week N | Phase 4 cloud>>

Your job (do in order, do not skip):

## 1. Load context
- Read the plan file and the relevant memory entries.
- `git -C /Users/ottimate/loop-revops-agents log --oneline -20` and `git status` to see what shipped since the last review.
- Pull the Monday board items + subitems for this phase (GraphQL: `items_page` with `subitems{id name column_values}`). Note which are Done vs open, and which have "Claude Session" empty.

## 2. Static review
- Re-read every module touched in this phase against the plan's acceptance criteria and the Cloud Portability Rules (no hardcoded `/Users/`, secrets via `shared/secrets`, DB via `shared/db/connection`, cron via `schedule.py`, Socket Mode only).
- Flag: dead code, missing error handling at system boundaries, governance bypasses, hardcoded channel IDs, unawaited coroutines, broken imports, schema drift.

## 3. Dynamic tests
- `cd /Users/ottimate/loop-revops-agents && source .venv/bin/activate && pytest -q --cov=shared --cov=agents --cov-report=term-missing`
- MCP smoke tests: `python -m shared.mcp.salesforce_mcp --smoke`, fireflies smoke (skip if key is REPLACE), `python -m shared.mcp.knowledge_mcp --smoke`.
- Run `python -m shared.lint.import_rules` against a deliberate fixture.
- Generate launchd plists to a tmp dir and diff against expected.
- Portability grep: `grep -rn "/Users/" shared/ agents/ --include='*.py'` must be empty.

## 4. Stress / adversarial
- **Governance:** `bulk_update` of 500 records without a gate must raise `ApprovalRequired` BEFORE any `_sf` call (patch `_sf` and assert `not_called`).
- **Rate limits:** fire `RATE_LIMITS['sf_writes_per_min']+1` writes in the same minute → last must be blocked.
- **Slack DEV_GUARD:** attempt send to a non-test channel with dev flag on → must refuse.
- **DB:** round-trip schema against Postgres if available (`docker run postgres:16`, set `REVOPS_DB_URL`, `alembic upgrade head`).
- **Classifier:** feed 15 canonical pain-signal strings and assert each routes to the right category.

## 5. Report to Monday
For each subitem reviewed:
- **Passes:** set status to `Done` and set the `Claude Session` column (`text_mm2cdvqv`) to a one-line verdict + today's date.
- **Fails / gap:** leave status as-is, set `Claude Session` to the specific defect + suggested fix (`file:line`), and create a new subitem titled `FIX: <one-line>` under the same parent with status `Not Started`.

Column IDs:
- Phase (grouping): `color_mm2cx5k2`
- **Task Status (update this one)**: `color_mm2ckrq0` — labels: `Not Started` / `Working on it` / `Done`
- Claude Session: `text_mm2cdvqv`
- Actual Done (date): `date_mm2cyn7m`
- Blast Radius: `color_mm2c95qv`
- Notes: `long_text_mm2cwz65`

## 6. Summary
Post a board update (`create_update`) on the phase's parent item with: tests run, pass count, coverage %, defects found, fixes filed as new subitems, Phase-N readiness verdict. Then print a ≤10-line summary to chat.

## Hard rules
- Do NOT mark a subitem Done without running the verification for it.
- Do NOT push SF writes in prod; all write paths must stay mocked or gated.
- If anything is ambiguous, stop and ask O before flipping statuses.
