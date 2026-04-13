"""Slack rendering for pre-demo briefs.

Emits two surfaces:
  - `to_slack_text(brief)` — markdown single-string, used by @oo sales-reps
    brief replies.
  - `to_slack_blocks(brief)` — Block Kit, used by the scheduler when DMing
    the AE 2h before the call.
"""
from __future__ import annotations

from typing import Any


def _pluralize(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def to_slack_text(brief: dict[str, Any]) -> str:
    acct = brief.get("account_name") or brief.get("opportunity_name") or "Unknown account"
    stage = brief.get("stage") or "—"
    amount = brief.get("amount")
    amount_txt = f"${amount:,.0f}" if amount else "—"
    close = brief.get("close_date") or "—"

    lines = [
        f"*Pre-demo brief — {acct}*",
        f"  · Stage: {stage}  · Amount: {amount_txt}  · Close: {close}",
    ]

    people = brief.get("people") or []
    if people:
        lines.append("\n*Who's on the call (and decision-makers to know)*")
        for p in people[:8]:
            tag = "✈️ attending" if p.get("attending") else "  committee"
            name = p.get("name") or p.get("email") or "?"
            title = p.get("title") or "—"
            li = p.get("linkedin_url")
            li_part = f" · <{li}|LinkedIn>" if li else ""
            lines.append(f"   - {tag} · *{name}* · {title}{li_part}")

    prior_calls = brief.get("prior_calls") or []
    if prior_calls:
        lines.append(f"\n*Prior conversations* ({_pluralize(len(prior_calls), 'call')})")
        for c in prior_calls[:3]:
            date = (c.get("date") or "")[:10]
            title = c.get("title") or "(untitled)"
            lines.append(f"   - {date} · {title}")

    news = brief.get("news") or []
    if news:
        lines.append("\n*Recent news*")
        for n in news[:3]:
            pub = n.get("source") or "web"
            url = n.get("url") or ""
            t = n.get("title") or "(no title)"
            lines.append(f"   - <{url}|{t}> · {pub}")

    funding = brief.get("funding") or []
    if funding:
        lines.append("\n*Funding*")
        for r in funding[:3]:
            amt = r.get("amount_usd")
            amt_txt = f"${amt:,.0f}" if amt else "—"
            when = r.get("announced_at") or "?"
            lines.append(f"   - {r.get('type') or 'round'} · {amt_txt} · {when}")

    kb_hits = brief.get("knowledge") or []
    if kb_hits:
        lines.append("\n*Internal notes (top matches)*")
        for k in kb_hits[:3]:
            snippet = (k.get("snippet") or "")[:180].replace("\n", " ")
            lines.append(f"   - {snippet}…")

    talking = brief.get("talking_points") or []
    if talking:
        lines.append("\n*Talking points*")
        for tp in talking:
            lines.append(f"   - {tp}")

    gaps = brief.get("gaps") or []
    if gaps:
        lines.append("\n*Gaps to close before the call*")
        for g in gaps:
            lines.append(f"   - {g}")

    return "\n".join(lines)


def to_slack_blocks(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Block Kit — only used by the scheduler DM path."""
    acct = brief.get("account_name") or brief.get("opportunity_name") or "Unknown"
    header = {"type": "header", "text": {"type": "plain_text", "text": f"Pre-demo: {acct}"}}
    body = {"type": "section", "text": {"type": "mrkdwn", "text": to_slack_text(brief)}}
    return [header, body]
