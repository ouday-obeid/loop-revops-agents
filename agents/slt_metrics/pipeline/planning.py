"""2026 business planning constants for SLT Revenue Metrics.

Ported from the legacy OUTBOUNDER revenue_model config.yaml. Pure data — no
I/O, no DB, no imports from `shared.mcp.*`. Consumers: excel_model sheets
(Quota, Forecast Summary, Monthly Revenue, Expansion, Funnel Metrics, Rep
Forecast), scorecards (AE/SDR attainment targets), and the rep_config
seeder under `scripts/seed_slt_rep_config.py`.

All values ported 2026-04-17 from:
  /Users/odayo/revenue_model/config.yaml  (Gaming PC, decommissioned)
Upstream source-of-truth spreadsheets:
  Financial Projections 2026 - Base 80% Final V1.xlsx
  Revenue Model Overview
  Sales Team Roster.xlsx
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


FISCAL_YEAR: Final[int] = 2026


# ---------------------------------------------------------------- annual targets

@dataclass(frozen=True)
class AnnualTargets:
    gross_new_arr: float        # New Customer ARR + Upsell
    net_new_arr: float          # New Customer ARR only
    expansion_arr: float        # Upsell / Expansion
    churn_rate: float           # fractional (0.0257 = 2.57%)
    starting_arr: float         # Jan 1 starting ARR
    monthly_churn_arr: float    # avg monthly churn $


ANNUAL_TARGETS: Final[AnnualTargets] = AnnualTargets(
    gross_new_arr=18_783_104.0,
    net_new_arr=17_083_104.0,
    expansion_arr=1_700_000.0,
    churn_rate=0.0257,
    starting_arr=9_041_808.0,
    monthly_churn_arr=19_329.0,
)


# ---------------------------------------------------------------- monthly ramp

@dataclass(frozen=True)
class MonthlyTarget:
    new_biz: float
    expansion: float


# Keyed 1..12. New biz ramps with headcount; expansion ramps monthly.
MONTHLY_TARGETS: Final[dict[int, MonthlyTarget]] = {
    1:  MonthlyTarget(new_biz=716_667.0,   expansion=90_032.0),
    2:  MonthlyTarget(new_biz=749_998.0,   expansion=94_053.0),
    3:  MonthlyTarget(new_biz=1_133_322.0, expansion=99_667.0),
    4:  MonthlyTarget(new_biz=1_324_983.0, expansion=106_941.0),
    5:  MonthlyTarget(new_biz=1_424_981.0, expansion=119_305.0),
    6:  MonthlyTarget(new_biz=1_524_978.0, expansion=131_799.0),
    7:  MonthlyTarget(new_biz=1_624_975.0, expansion=142_444.0),
    8:  MonthlyTarget(new_biz=1_716_640.0, expansion=148_219.0),
    9:  MonthlyTarget(new_biz=1_716_640.0, expansion=157_606.0),
    10: MonthlyTarget(new_biz=1_716_640.0, expansion=187_116.0),
    11: MonthlyTarget(new_biz=1_716_640.0, expansion=204_519.0),
    12: MonthlyTarget(new_biz=1_716_640.0, expansion=218_300.0),
}


def monthly_target(month: int, kind: str = "new_biz") -> float:
    """Return `new_biz` or `expansion` target for the given month (1..12)."""
    mt = MONTHLY_TARGETS.get(month)
    if mt is None:
        return 0.0
    return getattr(mt, kind, 0.0)


# ---------------------------------------------------------------- segment targets

# SMB / MM / ENT segment-level planning assumptions. `target_*` are planning
# values from Revenue Model Overview; `avg_deal_size` / `arpl` are actuals-
# based rollups from Location Analysis. Kept together here so the Pipeline
# by Segment sheet and the Funnel Metrics sheet read one source.

@dataclass(frozen=True)
class SegmentTargets:
    avg_deal_size: float       # ACV per customer (actuals)
    target_ads: float          # Planning target ACV per deal
    mix_pct: float             # % of new customer ARR
    arpl: float                # ARR per location (actuals)
    target_arpl: float         # ARPL target
    target_lpd: float          # Avg locations per deal target


SEGMENTS: Final[dict[str, SegmentTargets]] = {
    "SMB": SegmentTargets(
        avg_deal_size=8_076.0,  target_ads=10_000.0, mix_pct=0.2221,
        arpl=1_768.0,           target_arpl=2_285.0, target_lpd=3.5,
    ),
    "MM": SegmentTargets(
        avg_deal_size=36_174.0, target_ads=36_000.0, mix_pct=0.5899,
        arpl=1_270.0,           target_arpl=1_636.0, target_lpd=22.0,
    ),
    # Note: PC bot used "Ent"; Urkle pipeline/config.py uses "ENT" for segment
    # bands. Keep both spellings reachable via segment_targets() helper.
    "ENT": SegmentTargets(
        avg_deal_size=161_373.0, target_ads=100_000.0, mix_pct=0.1880,
        arpl=703.0,              target_arpl=1_000.0,  target_lpd=100.0,
    ),
}


def segment_targets(segment: str) -> SegmentTargets | None:
    """Case-insensitive lookup tolerating 'Ent' / 'ent' / 'ENT'."""
    if not segment:
        return None
    key = segment.upper()
    if key == "ENT":
        return SEGMENTS["ENT"]
    return SEGMENTS.get(key)


# ---------------------------------------------------------------- blended targets

@dataclass(frozen=True)
class BlendedTargets:
    arpl: float        # Blended ARPL target
    ads: float         # Blended ADS target - new business
    lpd: float         # Blended locations per deal target


BLENDED_TARGETS: Final[BlendedTargets] = BlendedTargets(
    arpl=1_640.0,
    ads=25_000.0,
    lpd=13.0,
)


# ---------------------------------------------------------------- quarterly funnel

# Deals needed per quarter per segment to hit revenue. Source: Revenue Model
# Overview rows 39-42. Keyed by ("Q1".."Q4", "SMB"|"MM"|"ENT") → float count.
QUARTERLY_FUNNEL_TARGETS: Final[dict[str, dict[str, float]]] = {
    "Q1": {"SMB": 73.13,  "MM": 34.15, "ENT": 4.54},
    "Q2": {"SMB": 123.86, "MM": 57.83, "ENT": 7.69},
    "Q3": {"SMB": 147.58, "MM": 68.91, "ENT": 9.16},
    "Q4": {"SMB": 150.36, "MM": 70.21, "ENT": 9.34},
}


def quarterly_funnel_target(quarter: str, segment: str) -> float:
    """Q1..Q4 × SMB/MM/ENT → float. Tolerates 'Ent'."""
    q = QUARTERLY_FUNNEL_TARGETS.get(quarter, {})
    return q.get(segment.upper(), 0.0)


# ---------------------------------------------------------------- rates

@dataclass(frozen=True)
class Rates:
    win_rate_new_biz: float
    win_rate_expansion: float
    hold_rate: float
    opps_per_win: float
    # phase → historical win rate; phases: "Early" | "Mid" | "Late"
    stage_win_rates: dict[str, float]


RATES: Final[Rates] = Rates(
    win_rate_new_biz=0.30,
    win_rate_expansion=0.95,
    hold_rate=0.50,
    opps_per_win=3.3,
    stage_win_rates={"Early": 0.10, "Mid": 0.30, "Late": 0.60},
)


# ---------------------------------------------------------------- seasonality

# Monthly seasonality index (1.0 = average month). Source: Revenue Model
# Forecast sheet, row 28.
SEASONALITY: Final[dict[int, float]] = {
    1:  0.50,  2:  0.60,  3:  0.90,  4:  1.00,
    5:  1.00,  6:  1.00,  7:  1.00,  8:  1.00,
    9:  1.00,  10: 1.00,  11: 1.00,  12: 1.33,
}


# ---------------------------------------------------------------- board targets

# Target thresholds rendered in the Board Metrics sheet's "Target" column.
# Source: SLT planning deck, 2026-04 revision. Change here to change the
# workbook — sheet reads these at render time, not via .xlsx hardcode.
NRR_TARGET: Final[float] = 1.10              # Net Revenue Retention
LOGO_RETENTION_TARGET: Final[float] = 0.90
EXPANSION_RATE_TARGET: Final[float] = 0.15


# ---------------------------------------------------------------- headcount

@dataclass(frozen=True)
class HeadcountPlan:
    starting: int      # Pre-Jan team size
    target: int        # EOY 2026 total GTM headcount
    ae_target: int
    sdr_target: int


HEADCOUNT: Final[HeadcountPlan] = HeadcountPlan(
    starting=19,
    target=42,
    ae_target=19,    # 2 Sr MM + 10 MM + 1 Sr Ent + 1 Ent + 5 SMB
    sdr_target=19,   # 17 SDR + 2 Enterprise SDR
)


# ---------------------------------------------------------------- roster

# `owner_name` must match SF `Owner.Name` exactly (rep_config PK). Quotas in
# USD annualized. Status: 'ramped' | 'ramping'. Segment: 'SMB' | 'MM' | 'ENT'
# (stored as `team` in rep_config). Role: 'AE' | 'SDR' | 'SDR Team Lead' |
# 'Manager'.

@dataclass(frozen=True)
class RosterEntry:
    name: str
    role: str
    segment: str       # maps to rep_config.team
    status: str
    annual_quota: float  # 0 for non-carriers (managers, SDRs)


AE_ROSTER: Final[tuple[RosterEntry, ...]] = (
    # Ramped AEs
    RosterEntry("Sarra Herlich",      "AE",      "MM",  "ramped",  1_000_000.0),
    RosterEntry("James Chavious",     "AE",      "MM",  "ramped",  1_000_000.0),
    RosterEntry("Alexis Marrero",     "AE",      "SMB", "ramped",  1_200_000.0),
    RosterEntry("Alex Reyes",         "AE",      "ENT", "ramped",  1_400_000.0),
    RosterEntry("Simon Salomon",      "AE",      "MM",  "ramped",  1_200_000.0),
    RosterEntry("Eric Azaren",        "AE",      "MM",  "ramped",  1_000_000.0),
    RosterEntry("Jessy Calderon",     "AE",      "MM",  "ramped",  1_000_000.0),
    # Ramping AEs
    RosterEntry("Devyn Schwartzberg", "AE",      "MM",  "ramping", 1_700_000.0),
    RosterEntry("Matt Bullock",       "AE",      "MM",  "ramping", 1_000_000.0),
    RosterEntry("Clay Arvizu",        "AE",      "MM",  "ramping", 1_000_000.0),
    RosterEntry("Taylor Ludwick",     "AE",      "MM",  "ramping", 1_000_000.0),
    RosterEntry("Daniel Varela",      "AE",      "MM",  "ramping", 1_000_000.0),
    RosterEntry("Carlton Ekiyor",     "AE",      "MM",  "ramping", 1_000_000.0),
    RosterEntry("Nick Barbo",         "AE",      "SMB", "ramping", 1_000_000.0),
    # Non-AE deal-owning managers
    RosterEntry("Charles Kagahastian", "Manager", "ENT", "ramped",  0.0),
)


SDR_ROSTER: Final[tuple[RosterEntry, ...]] = (
    # Team Leads
    RosterEntry("Brad Dressler",   "SDR Team Lead", "MM",  "ramped",  0.0),
    RosterEntry("Tyrell Belle",    "SDR Team Lead", "ENT", "ramped",  0.0),
    # Ramped SDRs
    RosterEntry("Chandler Zombek", "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("Danny Park",      "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("James Schrepel",  "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("Josue Sanchez",   "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("Olivia Johnson",  "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("Peter Milillo",   "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("Spencer Epps",    "SDR",           "MM",  "ramped",  0.0),
    RosterEntry("Vianny Gutierrez","SDR",           "MM",  "ramped",  0.0),
    # Ramping SDRs
    RosterEntry("Alexis Ribero",   "SDR",           "SMB", "ramping", 0.0),
    RosterEntry("Ben French",      "SDR",           "SMB", "ramping", 0.0),
)


# Manager → direct-report AE names. Used by the Rep Forecast sheet to group
# per-rep rows under their manager and by any future routing logic.
MANAGER_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "Nate":    ("Simon Salomon", "Eric Azaren", "Carlton Ekiyor", "Nick Barbo"),
    "Charles": ("Matt Bullock", "Clay Arvizu", "Daniel Varela",
                "Jessy Calderon", "Taylor Ludwick"),
    "Hutch":   ("Alexis Marrero", "James Chavious", "Sarra Herlich"),
    "IC":      ("Devyn Schwartzberg", "Alex Reyes"),
}


def manager_for_ae(name: str) -> str:
    """Return the manager name for a given AE owner_name, else 'Unassigned'."""
    for mgr, members in MANAGER_GROUPS.items():
        if name in members:
            return mgr
    return "Unassigned"


# ---------------------------------------------------------------- sanity checks

# Kept as module-level assertions (cheap — run once on import) so a typo in
# the ramp or seasonality surfaces at import time, not when Henry opens the
# workbook and sees a broken column.
assert set(MONTHLY_TARGETS.keys()) == set(range(1, 13)), "monthly ramp must cover 12 months"
assert set(SEASONALITY.keys()) == set(range(1, 13)),      "seasonality must cover 12 months"
assert set(QUARTERLY_FUNNEL_TARGETS.keys()) == {"Q1", "Q2", "Q3", "Q4"}
assert all(abs(sum(SEGMENTS[s].mix_pct for s in SEGMENTS) - 1.0) < 0.01 for _ in [1]), \
    "segment mix_pct should sum to ~1.0"
