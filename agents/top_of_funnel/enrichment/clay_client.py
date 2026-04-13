"""Clay.com client — decision-maker contact enrichment + credit budget.

Budget rules (non-negotiable):
  * 80% consumed → Slack alert to O's DM, enrichment CONTINUES
  * 100% consumed → `ClayBudgetExceeded`, enrichment HARD BLOCKED

Grade-B-or-higher floor (Loop global cost-control rule) is enforced per-call:
  grade below `clay_grade_floor` (default "B") returns None WITHOUT spending credits.

Budget ledger persists in `agents/top_of_funnel/state.db:clay_credit_ledger`.
One row per month-key (YYYY-MM). `spend()` is atomic.

HTTP to Clay is stubbed in D4 — the enrichment call path returns a deterministic
shape so the pipeline (D5) and tests can verify spend/skip/block behavior.
Real HTTP wiring ships in D4.5 or D5 once the Clay endpoint is confirmed with O.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from agents.top_of_funnel.state import get_state_engine

log = logging.getLogger(__name__)


GRADE_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1, "unavailable": 0}


class ClayBudgetExceeded(Exception):
    """Raised when a spend() call would push consumed past monthly_cap."""


@dataclass(frozen=True)
class ClayEnrichResult:
    email: str | None
    phone: str | None
    grade: str
    credits_used: int
    skipped: bool = False
    skip_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "phone": self.phone,
            "grade": self.grade,
            "credits_used": self.credits_used,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


# ------------------------------------------------------------------ CreditBudget


class CreditBudget:
    """Monthly Clay credit ledger with 80%/100% thresholds.

    `alert_callback(msg)` fires exactly once per threshold per month — the
    ledger records `alerted_80pct_at` / `alerted_100pct_at` so a second breach
    in the same month doesn't spam Slack.
    """

    ALERT_80 = 0.80
    ALERT_100 = 1.00

    def __init__(
        self,
        monthly_cap: int,
        *,
        alert_callback: Callable[[str], None] | None = None,
    ) -> None:
        if monthly_cap <= 0:
            raise ValueError(f"monthly_cap must be positive, got {monthly_cap}")
        self.monthly_cap = monthly_cap
        self._alert = alert_callback or (lambda msg: log.info("clay_budget: %s", msg))

    @classmethod
    def from_env(cls, *, alert_callback: Callable[[str], None] | None = None) -> "CreditBudget":
        raw = os.environ.get("CLAY_MONTHLY_BUDGET_CREDITS", "50000")
        try:
            cap = int(raw)
        except ValueError as e:
            raise ValueError(f"CLAY_MONTHLY_BUDGET_CREDITS={raw!r} is not an integer") from e
        return cls(cap, alert_callback=alert_callback)

    @staticmethod
    def _month_key(now: datetime | None = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m")

    def _ensure_row(self, month: str) -> None:
        engine = get_state_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """INSERT INTO clay_credit_ledger (month, consumed, cap)
                       VALUES (:m, 0, :cap)
                       ON CONFLICT(month) DO NOTHING"""
                ),
                {"m": month, "cap": self.monthly_cap},
            )

    def usage(self, month: str | None = None) -> int:
        month = month or self._month_key()
        self._ensure_row(month)
        engine = get_state_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT consumed FROM clay_credit_ledger WHERE month = :m"),
                {"m": month},
            ).fetchone()
        return int(row[0]) if row else 0

    def remaining(self, month: str | None = None) -> int:
        return max(0, self.monthly_cap - self.usage(month))

    def usage_pct(self, month: str | None = None) -> float:
        return self.usage(month) / self.monthly_cap if self.monthly_cap else 0.0

    def spend(self, credits: int, *, month: str | None = None) -> int:
        """Atomically add `credits` to the current month. Fires alerts on
        crossing 80% / 100%. Raises ClayBudgetExceeded on 100% breach —
        the increment is NOT applied so consumed never exceeds cap, but the
        100%-alerted marker IS committed so the second attempt doesn't re-spam."""
        if credits < 0:
            raise ValueError("credits must be >= 0")
        month = month or self._month_key()
        self._ensure_row(month)

        engine = get_state_engine()
        now = datetime.now(timezone.utc)
        cap_80 = self.monthly_cap * self.ALERT_80

        # Phase 1 — read + apply increment (or mark 100% and raise).
        # We split over-cap into its own committed transaction so the
        # alerted_100pct_at marker survives the raised exception.
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """SELECT consumed, alerted_80pct_at, alerted_100pct_at
                       FROM clay_credit_ledger WHERE month = :m"""
                ),
                {"m": month},
            ).fetchone()
            before = int(row[0])
            alerted_80 = row[1] is not None
            alerted_100 = row[2] is not None
            after = before + credits

            cross_80 = before < cap_80 <= after and not alerted_80
            hit_100_exact = after == self.monthly_cap and not alerted_100

            if after <= self.monthly_cap:
                conn.execute(
                    text(
                        """UPDATE clay_credit_ledger
                           SET consumed = :after,
                               alerted_80pct_at = CASE
                                   WHEN :mark_80 = 1 THEN :n ELSE alerted_80pct_at
                               END,
                               alerted_100pct_at = CASE
                                   WHEN :mark_100 = 1 THEN :n ELSE alerted_100pct_at
                               END
                           WHERE month = :m"""
                    ),
                    {
                        "after": after,
                        "mark_80": 1 if cross_80 else 0,
                        "mark_100": 1 if hit_100_exact else 0,
                        "n": now,
                        "m": month,
                    },
                )

        # Over-cap path — separate committed transaction, then raise.
        if after > self.monthly_cap:
            if not alerted_100:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """UPDATE clay_credit_ledger
                               SET alerted_100pct_at = :n
                               WHERE month = :m"""
                        ),
                        {"n": now, "m": month},
                    )
                self._alert(
                    f"*Clay credits 100% consumed* for {month}. "
                    f"Enrichment is now hard-blocked until you top up or next month begins. "
                    f"({before}/{self.monthly_cap} consumed; {credits} requested)"
                )
            raise ClayBudgetExceeded(
                f"cannot spend {credits}: would bring month {month} to "
                f"{after}/{self.monthly_cap}"
            )

        # Fire alerts after the DB commit succeeded.
        if cross_80:
            self._alert(
                f"*Clay credits 80% consumed* for {month}. "
                f"{after}/{self.monthly_cap} used; {self.monthly_cap - after} remaining. "
                "Enrichment continues; consider raising `clay_grade_floor` to `A` to slow burn."
            )
        if hit_100_exact:
            self._alert(
                f"*Clay credits 100% consumed* for {month}. "
                "Next enrichment attempt will hard-block."
            )

        return after


# ---------------------------------------------------------- grade floor helper


def _meets_floor(grade: str, floor: str) -> bool:
    return GRADE_ORDER.get(grade, 0) >= GRADE_ORDER.get(floor, 0)


# --------------------------------------------------------------- enrichment API


async def enrich_contact(
    *,
    domain: str,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    grade: str = "unavailable",
    budget: CreditBudget | None = None,
    grade_floor: str = "B",
    http_client: Any = None,
    credits_per_enrichment: int = 1,
) -> ClayEnrichResult:
    """Enrich one contact via Clay. Respects budget + grade floor.

    Returns `ClayEnrichResult(skipped=True, ...)` if grade is below floor —
    never spends credits in that case. Raises `ClayBudgetExceeded` if budget
    is 100% consumed.
    """
    if not _meets_floor(grade, grade_floor):
        return ClayEnrichResult(
            email=None,
            phone=None,
            grade=grade,
            credits_used=0,
            skipped=True,
            skip_reason=f"grade_below_floor (grade={grade}, floor={grade_floor})",
        )

    if budget is None:
        budget = CreditBudget.from_env()

    # Atomic pre-spend — raises if we'd cross 100%.
    budget.spend(credits_per_enrichment)

    # Real HTTP to Clay ships in D4.5 / D5 (placeholder below).
    payload = await _call_clay_api(
        domain=domain,
        first_name=first_name,
        last_name=last_name,
        title=title,
        http_client=http_client,
    )

    return ClayEnrichResult(
        email=payload.get("email"),
        phone=payload.get("phone"),
        grade=payload.get("grade") or grade,
        credits_used=credits_per_enrichment,
        raw=payload,
    )


async def _call_clay_api(
    *,
    domain: str,
    first_name: str | None,
    last_name: str | None,
    title: str | None,
    http_client: Any = None,
) -> dict[str, Any]:
    """D4 stub — returns deterministic shape so the pipeline (D5) and tests
    can proceed. Real HTTP ships when Clay endpoint is confirmed with O."""
    if http_client is None:
        return {"email": None, "phone": None, "grade": "unavailable", "stub": True}
    # Production: POST to Clay, raise-or-retry on 4xx/5xx, return their JSON.
    raise NotImplementedError("Real Clay HTTP wiring is deferred to D4.5")


# ------------------------------------------------------------------ Slack entry


async def credit_status() -> dict[str, Any]:
    """Slack entry: `@oo tof credits` — shows used / cap / remaining / pct."""
    budget = CreditBudget.from_env()
    month = CreditBudget._month_key()
    used = budget.usage(month)
    remaining = budget.remaining(month)
    pct = budget.usage_pct(month) * 100
    bar_width = 20
    filled = int(bar_width * pct / 100) if pct <= 100 else bar_width
    bar = "█" * filled + "░" * (bar_width - filled)
    return {
        "text": (
            f"*Clay credits — {month}*\n"
            f"`{bar}` {pct:.1f}%\n"
            f"Used: {used:,} / {budget.monthly_cap:,}  |  Remaining: {remaining:,}"
        ),
        "used": used,
        "cap": budget.monthly_cap,
        "remaining": remaining,
        "pct": pct,
    }
