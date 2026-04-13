"""ICP Scorer — 100-point restaurant-group model.

Pure function. `score_account(account_data)` returns an ICPScore dataclass.
Weights live in config/icp_weights.yaml (versioned, recalibrated quarterly);
knobs (tier bands, exploration floor, grade floor) live in config/icp_config.yaml.

Dimensions (100 pts base):
  Ownership       25 — franchise_group_multi_brand through independent
  Locations       25 — 50+ / 10-49 / 3-9 / 1-2 bands
  Brand Vertical  20 — QSR / Fast_Casual / Casual_Dining / Fine_Dining / Other
  Growth Signals  20 — recent_ma_activity / funding_round / new_openings
  Tech-stack Fit  10 — POS + loyalty + delivery integrations

Bonus (uncapped above base):
  Product Attach  +5 — account is an existing Loop customer expanding to another brand
                      (so a Loop customer scoring 73+5=78 beats a cold 77)

Tiers (default from config/icp_config.yaml):
  A  total >= 70   — top-of-queue, ENT treatment, full enrichment
  B  total >= 45   — briefing inclusion, enrichment proceeds
  C  total >= 25   — exploration-slot eligible, no Clay enrichment
  D  total < 25    — skipped
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml


Tier = Literal["A", "B", "C", "D"]

_AGENT_DIR = Path(__file__).parent
_WEIGHTS_PATH = _AGENT_DIR / "config" / "icp_weights.yaml"
_CONFIG_PATH = _AGENT_DIR / "config" / "icp_config.yaml"


@dataclass(frozen=True)
class ICPScore:
    total: int
    tier: Tier
    signals: dict[str, int] = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "tier": self.tier,
            "signals": dict(self.signals),
            "explanation": self.explanation,
        }


@lru_cache(maxsize=4)
def _load_yaml_cached(path_str: str, mtime_ns: int) -> dict[str, Any]:
    with open(path_str, "rb") as f:
        return yaml.safe_load(f) or {}


def _load(path: Path) -> dict[str, Any]:
    return _load_yaml_cached(str(path), path.stat().st_mtime_ns)


def _score_ownership(account: dict[str, Any], weights: dict[str, Any]) -> int:
    bands = weights.get("dimensions", {}).get("ownership", {}).get("bands", {})
    key = account.get("ownership_type")
    return int(bands.get(key, 0))


def _score_location_count(account: dict[str, Any], weights: dict[str, Any]) -> int:
    bands = weights.get("dimensions", {}).get("location_count", {}).get("bands", []) or []
    count = account.get("location_count")
    if count is None:
        return 0
    try:
        count = int(count)
    except (TypeError, ValueError):
        return 0
    for band in bands:
        if count >= int(band.get("min", 0)):
            return int(band.get("points", 0))
    return 0


def _score_brand_vertical(account: dict[str, Any], weights: dict[str, Any]) -> int:
    bands = weights.get("dimensions", {}).get("brand_vertical", {}).get("bands", {})
    key = account.get("brand_vertical")
    return int(bands.get(key, 0))


def _score_growth_signals(account: dict[str, Any], weights: dict[str, Any]) -> int:
    spec = weights.get("dimensions", {}).get("growth_signals", {})
    components = spec.get("components", {})
    cap = int(spec.get("max", 0))
    signals = account.get("growth_signals") or {}
    total = sum(int(pts) for key, pts in components.items() if signals.get(key))
    return min(total, cap)


def _score_tech_stack(account: dict[str, Any], weights: dict[str, Any]) -> int:
    spec = weights.get("dimensions", {}).get("tech_stack_fit", {})
    components = spec.get("components", {})
    cap = int(spec.get("max", 0))
    stack = account.get("tech_stack") or []
    if isinstance(stack, dict):
        keys = {k for k, v in stack.items() if v}
    else:
        keys = set(stack)
    total = sum(int(pts) for key, pts in components.items() if key in keys)
    return min(total, cap)


def _score_product_attach(account: dict[str, Any], weights: dict[str, Any]) -> int:
    bonus = weights.get("bonuses", {}).get("product_attach", {})
    products = account.get("current_loop_products") or []
    return int(bonus.get("points", 0)) if products else 0


def _tier_for(total: int, config: dict[str, Any], weights: dict[str, Any]) -> Tier:
    tiers = weights.get("tiers", {})
    a = int(tiers.get("A_threshold", 70))
    b = int(tiers.get("B_threshold", 45))
    c = int(tiers.get("exploration_floor", 25))
    if total >= a:
        return "A"
    if total >= b:
        return "B"
    if total >= c:
        return "C"
    return "D"


def _explain(account: dict[str, Any], signals: dict[str, int], total: int, tier: Tier) -> str:
    domain = account.get("domain") or account.get("Website") or "?"
    parts = [
        f"*ICP {total}/100 — tier {tier}* for `{domain}`",
        (
            f"• Ownership {signals['ownership']}/25"
            f" ({account.get('ownership_type') or '—'})"
        ),
        (
            f"• Locations {signals['location_count']}/25"
            f" ({account.get('location_count') or '—'})"
        ),
        (
            f"• Vertical {signals['brand_vertical']}/20"
            f" ({account.get('brand_vertical') or '—'})"
        ),
        f"• Growth signals {signals['growth_signals']}/20",
        f"• Tech-stack fit {signals['tech_stack_fit']}/10",
    ]
    if signals.get("product_attach"):
        parts.append(
            f"• Product-attach bonus +{signals['product_attach']}"
            f" ({', '.join(account.get('current_loop_products') or [])})"
        )
    return "\n".join(parts)


def score_account(
    account: dict[str, Any],
    *,
    weights_path: Path | None = None,
    config_path: Path | None = None,
) -> ICPScore:
    """Score one account. Pure — no I/O beyond reading YAML (cached by mtime)."""
    weights = _load(weights_path or _WEIGHTS_PATH)
    config = _load(config_path or _CONFIG_PATH)

    signals = {
        "ownership": _score_ownership(account, weights),
        "location_count": _score_location_count(account, weights),
        "brand_vertical": _score_brand_vertical(account, weights),
        "growth_signals": _score_growth_signals(account, weights),
        "tech_stack_fit": _score_tech_stack(account, weights),
        "product_attach": _score_product_attach(account, weights),
    }
    total = sum(signals.values())
    tier = _tier_for(total, config, weights)
    explanation = _explain(account, signals, total, tier)
    return ICPScore(total=total, tier=tier, signals=signals, explanation=explanation)


async def score_domain(domain: str) -> dict[str, Any]:
    """Slack entry: `@oo tof score <domain>`.

    D2: scores against the most recent `tof_lead_candidates` row for this domain
    if one exists. Apollo-backed lookup ships D4 (until then, returns an
    explanation asking the user to run `@oo tof enrich <domain>` first).
    """
    from sqlalchemy import text
    from agents.top_of_funnel.state import get_state_engine

    try:
        engine = get_state_engine()
    except Exception:
        return {"text": f"`score` — agent state DB unavailable for `{domain}`."}

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT account_payload FROM tof_lead_candidates
                   WHERE domain = :d
                   ORDER BY created_at DESC LIMIT 1"""
            ),
            {"d": domain.lower().strip()},
        ).fetchone()

    if row is None:
        return {
            "text": (
                f"No cached firmographics for `{domain}`. "
                "Apollo lookup ships D4 — for now, run `@oo tof enrich <domain>` first."
            )
        }

    import json

    account = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    account.setdefault("domain", domain)
    result = score_account(account)
    return {"text": result.explanation, "score": result.to_dict()}
