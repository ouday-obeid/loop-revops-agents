"""Typed payload shapes shared across pipeline / forecast / scorecards / excel.

Dataclasses stay deliberately plain (no validators, no ORMs). The scorer and
snapshotter operate on these; the Excel builder consumes `RevenueModelPayload`
as its single argument; briefings pull from `ForecastRollup` + `MoverSet`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


# ------------------------------------------------------------------ raw SF rows

@dataclass
class ContactRole:
    contact_id: str
    name: str | None
    email: str | None
    title: str | None
    role: str | None
    is_primary: bool = False


@dataclass
class OppRecord:
    """Denormalized Opportunity row as pulled from SOQL (see pipeline.fetcher).

    Kept wide on purpose — scoring, risk flags, and the Excel builder all read
    from the same record, and carrying one dict everywhere beats threading
    N typed fields through every caller.
    """
    id: str
    name: str
    account_id: str | None
    account_name: str | None
    account_website: str | None
    account_type: str | None
    owner_id: str | None
    owner_name: str | None
    owner_role: str | None
    owner_manager: str | None
    stage: str
    is_closed: bool
    is_won: bool
    amount: float | None
    acv: float | None
    fixed_arr: float | None
    locations: int | None
    type: str | None
    lead_source: str | None
    close_date: date | None
    created_date: datetime | None
    last_activity_date: date | None
    last_modified_date: datetime | None
    last_stage_change_date: date | None
    days_since_stage_change: int | None
    time_in_stage: int | None
    probability_sf: float | None
    description: str | None
    next_steps: str | None
    next_step_date: date | None
    icp_score: float | None
    segment: str | None
    products: dict[str, int] = field(default_factory=dict)
    contact_roles: list[ContactRole] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ forecast

@dataclass(frozen=True)
class ForecastWeights:
    """5-pillar weights. Seed values locked 2026-04-13 per scoping doc.

    Rep performance is NOT a composite pillar (surfaced on AE scorecards
    instead). ICP null proxies are capped at 0.5 inside the ICP pillar.
    """
    icp: float = 0.25
    stage: float = 0.30
    activity: float = 0.15
    timeline: float = 0.15
    call: float = 0.15
    version: str = "v1-seed"


@dataclass
class PillarScore:
    value: float          # 0.0–1.0
    detail: str           # short provenance string shown in Deal Details


@dataclass
class ScoredDeal:
    opp_id: str
    opp_name: str
    owner_name: str | None
    account_name: str | None
    segment: str | None
    stage: str
    amount: float | None
    acv: float | None
    close_date: date | None
    score: int                    # 0–100 (round of weighted pillar sum * 100)
    probability: float            # 0.0–1.0
    category: str                 # Strong Commit / Commit / High Confidence / Longshot / Pipe Dream
    weighted_acv: float           # acv * probability
    pillars: dict[str, PillarScore]
    risk_flags: list[str]
    weights_version: str
    raw: OppRecord | None = None


@dataclass
class ForecastRollup:
    """Per-quarter forecast rollup: commit, best-case, weighted."""
    horizon_quarter: str           # "FY2026-Q2"
    commit_amount: float           # Σ ACV where score ≥ 80
    best_case_amount: float        # Σ ACV where score ≥ 50
    weighted_amount: float         # Σ (ACV × probability)
    deal_count: int
    by_owner: dict[str, dict[str, float]] = field(default_factory=dict)
    by_segment: dict[str, dict[str, float]] = field(default_factory=dict)


# ------------------------------------------------------------------ movers

@dataclass
class Mover:
    opp_id: str
    opp_name: str
    owner_name: str | None
    kind: str                      # "new", "advanced", "pushed", "slipped", "lost", "won", "amount_up", "amount_down"
    before: dict[str, Any]
    after: dict[str, Any]
    delta_acv: float | None = None
    delta_days: int | None = None


@dataclass
class MoverSet:
    period_from: date
    period_to: date
    movers: list[Mover] = field(default_factory=list)

    def top(self, n: int = 10) -> list[Mover]:
        return sorted(
            self.movers,
            key=lambda m: abs(m.delta_acv or 0),
            reverse=True,
        )[:n]


# ------------------------------------------------------------------ scorecards

@dataclass
class AeCard:
    rep_email: str
    rep_name: str | None
    attainment_pct: float | None
    close_rate_pct: float | None
    avg_cycle_days: float | None
    avg_acv: float | None
    pipeline_created: float
    pipeline_advanced: float
    call_grade_avg: float | None
    rep_perf_score: int | None     # data column, not a composite weight
    deals_open: int
    deals_commit: int


@dataclass
class SdrCard:
    sdr_email: str
    sdr_name: str | None
    dials: int | None
    connects: int | None
    meetings_set: int
    meetings_held: int
    pipeline_sourced: float
    pipeline_advanced: float
    leaderboard_rank: int | None


# ------------------------------------------------------------------ board / unit econ

@dataclass
class UnitEconomics:
    """All fields nullable. When Loop Pulse (BigQuery) unavailable, every cell is
    None and gap_flag is True — the Excel builder renders '-- (Loop Pulse unavailable)'.
    """
    gross_revenue_retention: float | None
    net_revenue_retention: float | None
    logo_retention: float | None
    expansion_rate: float | None
    cac_payback_months: float | None
    ltv_cac_ratio: float | None
    gap_flag: bool = False


@dataclass
class BoardMetrics:
    as_of: date
    arr: float | None
    nrr: float | None
    logo_retention: float | None
    expansion_rate: float | None
    pipeline_coverage_mm: float | None   # target 3x
    pipeline_coverage_ent: float | None  # target 4x
    unit_economics: UnitEconomics


# ------------------------------------------------------------------ call intel

@dataclass
class CallIntelSignal:
    opp_id: str
    transcripts_considered: int
    keyword_hits: list[str]
    champion_present: bool
    rep_action_items: int
    negative_hits: list[str]
    classifier_verdict: dict[str, Any] | None   # Haiku output for top-20 opps, else None
    score_delta: float                          # final contribution to the call pillar


# ------------------------------------------------------------------ composite

@dataclass
class RevenueModelPayload:
    """Single argument to excel_model.builder.build(). Also drives briefings."""
    run_date: date
    horizon_quarter: str
    weights: ForecastWeights
    scored_deals: list[ScoredDeal]
    forecast_rollup: ForecastRollup
    movers: MoverSet
    ae_cards: list[AeCard]
    sdr_cards: list[SdrCard]
    board_metrics: BoardMetrics
    notes: list[str] = field(default_factory=list)
