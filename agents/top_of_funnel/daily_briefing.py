"""Daily Briefing — 07:55 Mon–Fri SDR lead-list DMs + dept-lead summary.

Cron entry: `send_daily_briefing()` (registered in shared.runtime.schedule via
Phase 0 amendment PR).

Flow:
  1. Find latest tof_enrichment_runs row; staleness-guard (>4h old or still
     running → DM O + bail; no 0-lead spam to SDRs).
  2. Load tof_lead_candidates for that run with status='ready'.
  3. Group by assigned_sdr_id; sort each bucket by icp_score DESC.
  4. Per SDR: DM with top 20 in body + full up-to-200 as thread replies.
  5. Exploration slot: include up to 20 tier C/D rows (ICP >= 40) so the
     model doesn't become self-reinforcing. Counts separately.
  6. Summary DM to each `summary_recipients` addr (Hutch for Phase 1):
     per-SDR tallies + top-5 overall + exploration counts.
  7. Mark all briefed candidates status='briefed' — rerun-safe.

Why serial DB writes: briefing runs on the 07:55 cron, one-at-a-time; parallel
SDRs aren't worth the complexity when there are <=10 of them.

Public surface:
  send_daily_briefing(*, send_fn=..., now=None, stale_threshold_hours=4)
      → dict[str, Any]  (briefing report — what was sent, to whom, counts)
  send_dry_run(channel) → dict[str, Any]
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import text

from agents.top_of_funnel import routing
from agents.top_of_funnel.state import get_state_engine

log = logging.getLogger(__name__)

_AGENT_NAME = "top_of_funnel"
_TOP_N_IN_BODY = 20
_MAX_THREAD = 200
_EXPLORATION_MAX = 20
_EXPLORATION_MIN_SCORE = 40
_O_DM_CHANNEL = "U08K2UTG3G8"  # O's Slack DM — stale-guard fallback


# -------------------------------------------------------------- latest run


def _latest_run(now: datetime) -> dict[str, Any] | None:
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT run_id, started_at, completed_at, status,
                          scanned, suppressed, written_count
                   FROM tof_enrichment_runs
                   ORDER BY id DESC LIMIT 1"""
            )
        ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row[0],
        "started_at": row[1],
        "completed_at": row[2],
        "status": row[3],
        "scanned": row[4] or 0,
        "suppressed": row[5] or 0,
        "written_count": row[6] or 0,
    }


def _is_stale(run: dict[str, Any], *, now: datetime, threshold_hours: int) -> tuple[bool, str]:
    """Return (stale, reason). True means briefing should bail + DM O."""
    if run is None:
        return True, "no_pipeline_run_found"
    if run.get("status") == "running":
        return True, f"pipeline_still_running (run_id={run['run_id']})"
    completed = run.get("completed_at")
    if completed is None:
        return True, f"pipeline_no_completion_ts (run_id={run['run_id']})"
    if isinstance(completed, str):
        try:
            completed = datetime.fromisoformat(completed)
        except ValueError:
            return True, f"pipeline_bad_completion_ts (run_id={run['run_id']})"
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    if (now - completed) > timedelta(hours=threshold_hours):
        return True, (
            f"pipeline_stale (run_id={run['run_id']}, "
            f"completed={completed.isoformat()}, now={now.isoformat()})"
        )
    return False, ""


# ---------------------------------------------------------- candidate query


def _load_candidates(run_id: str) -> list[dict[str, Any]]:
    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT id, domain, company_name, email, first_name, last_name,
                          title, icp_score, icp_tier, location_count, brand,
                          ownership_type, assigned_sdr_id, sf_lead_id
                   FROM tof_lead_candidates
                   WHERE run_id = :r AND status = 'ready'
                   ORDER BY assigned_sdr_id ASC, icp_score DESC"""
            ),
            {"r": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def _mark_briefed(candidate_ids: list[int]) -> None:
    if not candidate_ids:
        return
    engine = get_state_engine()
    with engine.begin() as conn:
        for cid in candidate_ids:
            conn.execute(
                text("UPDATE tof_lead_candidates SET status = 'briefed' WHERE id = :id"),
                {"id": cid},
            )


# -------------------------------------------------------------- grouping


def _group_by_sdr(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Return (primary_by_sdr, exploration_pool).

    primary: tier A/B per SDR, all of them (capped downstream).
    exploration_pool: tier C/D with icp_score >= _EXPLORATION_MIN_SCORE —
    distributed evenly across SDRs downstream to mitigate filter bias.
    Suppressed & unrouted rows are skipped from both.
    """
    primary: dict[str, list[dict[str, Any]]] = {}
    exploration: list[dict[str, Any]] = []
    for c in candidates:
        tier = (c.get("icp_tier") or "").upper()
        score = int(c.get("icp_score") or 0)
        sdr = c.get("assigned_sdr_id")
        if tier in ("A", "B"):
            # Primary requires an assigned SDR — unrouted A/B is a data error.
            if not sdr:
                continue
            primary.setdefault(sdr, []).append(c)
        elif tier in ("C", "D") and score >= _EXPLORATION_MIN_SCORE:
            # Exploration pool is shared; distributed round-robin downstream
            # across SDRs who received primary leads — SDR assignment optional.
            exploration.append(c)
    return primary, exploration


def _allocate_exploration(
    exploration: list[dict[str, Any]],
    sdrs: list[str],
    *,
    per_sdr: int = _EXPLORATION_MAX,
) -> dict[str, list[dict[str, Any]]]:
    """Round-robin the exploration pool across known SDRs, up to `per_sdr` each."""
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in sdrs}
    if not sdrs or not exploration:
        return out
    exp_sorted = sorted(exploration, key=lambda r: int(r.get("icp_score") or 0), reverse=True)
    i = 0
    for row in exp_sorted:
        sdr = sdrs[i % len(sdrs)]
        if len(out[sdr]) < per_sdr:
            out[sdr].append(row)
        i += 1
    return out


# ---------------------------------------------------------- block kit


def _lead_line(c: dict[str, Any], *, rank: int | None = None) -> str:
    """One-line summary for a lead row."""
    prefix = f"{rank}. " if rank else ""
    company = c.get("company_name") or c.get("domain") or "?"
    score = c.get("icp_score") or 0
    tier = c.get("icp_tier") or "?"
    loc = c.get("location_count")
    loc_str = f" · {loc} loc" if loc else ""
    brand = c.get("brand") or ""
    brand_str = f" · {brand}" if brand else ""
    owner = c.get("ownership_type") or ""
    owner_str = f" · {owner}" if owner else ""
    who = ""
    if c.get("first_name") or c.get("last_name"):
        name = f"{c.get('first_name','').strip()} {c.get('last_name','').strip()}".strip()
        title = c.get("title") or ""
        who = f" — {name}" + (f" ({title})" if title else "")
    return (
        f"{prefix}*{company}* — Tier {tier} · ICP {score}{loc_str}{brand_str}{owner_str}{who}"
    )


def _build_sdr_blocks(
    sdr_email: str,
    primary: list[dict[str, Any]],
    exploration: list[dict[str, Any]],
    run: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (body_blocks, thread_messages). Thread is plain text per lead."""
    header = (
        f"*Top of Funnel — Daily Briefing*\n"
        f"Run `{run['run_id']}`  ·  {len(primary)} prioritized + "
        f"{len(exploration)} exploration for `{sdr_email}`"
    )
    top_lines = [_lead_line(c, rank=i + 1) for i, c in enumerate(primary[:_TOP_N_IN_BODY])]
    body_blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]
    if top_lines:
        body_blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(top_lines)}}
        )
    else:
        body_blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No prioritized leads for you today._"}}
        )
    if exploration:
        exp_lines = [_lead_line(c) for c in exploration[:_EXPLORATION_MAX]]
        body_blocks.append({"type": "divider"})
        body_blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Exploration slot ({len(exp_lines)})* — below ICP threshold but worth a look:\n"
                    + "\n".join(exp_lines),
                },
            }
        )

    # Thread: full list beyond top 20, capped at _MAX_THREAD.
    thread: list[str] = []
    rest = primary[_TOP_N_IN_BODY:_MAX_THREAD]
    if rest:
        lines = [_lead_line(c, rank=i + _TOP_N_IN_BODY + 1) for i, c in enumerate(rest)]
        # Chunk to <=30 lines per thread message so Slack doesn't truncate.
        for i in range(0, len(lines), 30):
            thread.append("\n".join(lines[i : i + 30]))
    return body_blocks, thread


def _build_summary_blocks(
    per_sdr: dict[str, dict[str, Any]],
    all_primary: list[dict[str, Any]],
    run: dict[str, Any],
) -> list[dict[str, Any]]:
    lines = [
        f"*Top of Funnel — Overnight Summary*  `{run['run_id']}`",
        f"Scanned {run['scanned']} · Suppressed {run['suppressed']} · "
        f"Written {run['written_count']}",
        "",
        "*Per-SDR counts (primary / exploration):*",
    ]
    for sdr_email in sorted(per_sdr.keys()):
        info = per_sdr[sdr_email]
        lines.append(f"• `{sdr_email}` — {info['primary']} / {info['exploration']}")
    lines.append("")
    lines.append("*Top 5 overall by ICP score:*")
    top5 = sorted(all_primary, key=lambda r: int(r.get("icp_score") or 0), reverse=True)[:5]
    for i, c in enumerate(top5, 1):
        lines.append(_lead_line(c, rank=i))
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]


# -------------------------------------------------------- sender wiring


def _default_send(channel: str, text_: str, blocks: list | None = None, *, thread_ts: str | None = None) -> dict[str, Any]:
    """Send via shared.slack_dispatcher.SlackSender. Returns the SF response dict."""
    from shared.slack_dispatcher import SlackSender  # deferred to keep import cheap in tests

    sender = SlackSender()
    if thread_ts:
        # SlackSender.send doesn't support threads yet — fall back to client.
        client = sender._ensure_client()
        return client.chat_postMessage(
            channel=channel, text=text_, blocks=blocks, thread_ts=thread_ts
        )
    return sender.send(channel, text_, blocks)


def _resolve_slack_id(
    sdr_email: str,
    territory_cfg: dict[str, Any],
) -> str | None:
    """Find an SDR's Slack user-id from territory.yaml. Skips PLACEHOLDER."""
    for segment_cfg in (territory_cfg.get("segments") or {}).values():
        for entry in segment_cfg.get("rotation") or []:
            if (entry.get("email") or "").lower() == sdr_email.lower():
                slack_id = entry.get("slack_id")
                if slack_id and slack_id != "PLACEHOLDER":
                    return slack_id
    return None


def _user_id_to_email() -> dict[str, str]:
    """Read tof_sf_user_cache → {user_id: email}. Active or not — briefing
    surfaces the human name; actual routing already filtered inactive."""
    routing._ensure_user_cache()
    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT user_id, email FROM tof_sf_user_cache")).fetchall()
    return {r[0]: r[1] for r in rows}


# --------------------------------------------------------- public entrypoints


async def send_daily_briefing(
    *,
    send_fn: Callable[..., dict[str, Any]] | None = None,
    now: datetime | None = None,
    stale_threshold_hours: int = 4,
    territory_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cron entrypoint — 07:55 Mon–Fri. Returns a report dict.

    Args (all optional, injected in tests):
      send_fn: (channel, text, blocks=None, thread_ts=None) → dict
      now: override clock (default: UTC now)
      stale_threshold_hours: skip SDR DMs + warn O if pipeline run older (default 4)
      territory_cfg: loaded territory.yaml (default: loaded from disk)
    """
    now = now or datetime.now(timezone.utc)
    send = send_fn or _default_send
    territory = territory_cfg or routing.load_territory()

    run = _latest_run(now)
    stale, reason = _is_stale(run, now=now, threshold_hours=stale_threshold_hours)
    if stale:
        msg = (
            ":warning: *ToF Daily Briefing skipped*\n"
            f"Reason: `{reason}`. SDR DMs not sent to avoid 0-lead noise."
        )
        try:
            send(_O_DM_CHANNEL, msg)
        except Exception as exc:  # noqa: BLE001
            log.error("stale-guard DM to O failed: %s", exc)
        return {
            "status": "skipped",
            "reason": reason,
            "run_id": (run or {}).get("run_id"),
            "sent": 0,
        }

    candidates = _load_candidates(run["run_id"])
    primary_by_sdr, exploration_pool = _group_by_sdr(candidates)

    known_sdrs = sorted(primary_by_sdr.keys())
    exploration_by_sdr = _allocate_exploration(exploration_pool, known_sdrs)

    id_to_email = _user_id_to_email()

    per_sdr_summary: dict[str, dict[str, Any]] = {}
    all_briefed_ids: list[int] = []
    dms_sent = 0
    unresolved: list[str] = []

    for sdr_user_id, primary in primary_by_sdr.items():
        sdr_email = id_to_email.get(sdr_user_id, f"<unknown:{sdr_user_id}>")
        exploration = exploration_by_sdr.get(sdr_user_id, [])
        slack_id = _resolve_slack_id(sdr_email, territory)
        per_sdr_summary[sdr_email] = {
            "primary": len(primary),
            "exploration": len(exploration),
            "user_id": sdr_user_id,
            "slack_id": slack_id,
        }
        if slack_id is None:
            unresolved.append(sdr_email)
            # Still collect ids so we don't rebrief tomorrow for the same run.
            all_briefed_ids.extend(c["id"] for c in primary + exploration)
            continue

        blocks, thread_msgs = _build_sdr_blocks(sdr_email, primary, exploration, run)
        try:
            resp = send(slack_id, f"ToF daily briefing for {sdr_email}", blocks)
            parent_ts = (resp or {}).get("ts")
            for tm in thread_msgs:
                if parent_ts:
                    send(slack_id, tm, None, thread_ts=parent_ts)
                else:
                    # Fallback: send as separate DMs if parent ts missing.
                    send(slack_id, tm)
            dms_sent += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("failed briefing DM for %s: %s", sdr_email, exc)
            per_sdr_summary[sdr_email]["error"] = str(exc)
            continue

        all_briefed_ids.extend(c["id"] for c in primary + exploration)

    # Summary DMs to `summary_recipients` (e.g., Hutch).
    summary_recipients = territory.get("summary_recipients") or []
    all_primary_flat = [c for rows in primary_by_sdr.values() for c in rows]
    summary_blocks = _build_summary_blocks(per_sdr_summary, all_primary_flat, run)
    summary_sent: list[str] = []
    for email in summary_recipients:
        slack_id = _resolve_slack_id(email, territory)
        if slack_id is None:
            unresolved.append(f"summary:{email}")
            continue
        try:
            send(slack_id, "ToF overnight summary", summary_blocks)
            summary_sent.append(email)
        except Exception as exc:  # noqa: BLE001
            log.warning("summary DM to %s failed: %s", email, exc)

    _mark_briefed(all_briefed_ids)

    return {
        "status": "success",
        "run_id": run["run_id"],
        "sent": dms_sent,
        "summary_sent": summary_sent,
        "per_sdr": per_sdr_summary,
        "unresolved": unresolved,
        "briefed_count": len(all_briefed_ids),
    }


async def send_dry_run(
    channel: str,
    *,
    send_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Route ALL briefings to a single channel (O's DM by default) for preview.

    Renders the same blocks but doesn't mark candidates as briefed.
    """
    send = send_fn or _default_send
    territory = routing.load_territory()
    now = datetime.now(timezone.utc)
    run = _latest_run(now)
    if run is None:
        send(channel, "_No ToF pipeline run found — nothing to preview._")
        return {"status": "no_run", "sent": 0}

    candidates = _load_candidates(run["run_id"])
    primary_by_sdr, exploration_pool = _group_by_sdr(candidates)
    known_sdrs = sorted(primary_by_sdr.keys())
    exploration_by_sdr = _allocate_exploration(exploration_pool, known_sdrs)
    id_to_email = _user_id_to_email()

    previews = 0
    for sdr_user_id, primary in primary_by_sdr.items():
        sdr_email = id_to_email.get(sdr_user_id, f"<unknown:{sdr_user_id}>")
        exploration = exploration_by_sdr.get(sdr_user_id, [])
        blocks, _thread = _build_sdr_blocks(sdr_email, primary, exploration, run)
        send(channel, f"[DRY-RUN] Would DM `{sdr_email}`", blocks)
        previews += 1

    return {"status": "dry_run", "run_id": run["run_id"], "previews": previews}
