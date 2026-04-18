"""All KPI calculations and aggregations with lazy caching."""

from __future__ import annotations

from datetime import datetime
from functools import cached_property

import pandas as pd

from core.config_schema import Config

MONTHS = list(range(1, 13))
MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


class Processor:
    """Central computation engine. All properties are lazily cached."""

    def __init__(self, df: pd.DataFrame, cfg: Config):
        self.df = df
        self.cfg = cfg
        self.fiscal_year = cfg.fiscal_year
        self._current_month = datetime.now().month
        self.rep_forecast = None  # Set externally when --forecast is provided

    # ------------------------------------------------------------------
    # Filtered views
    # ------------------------------------------------------------------
    @cached_property
    def new_biz(self) -> pd.DataFrame:
        return self.df[self.df["is_new_business"]].copy()

    @cached_property
    def expansion(self) -> pd.DataFrame:
        return self.df[self.df["is_expansion"]].copy()

    @cached_property
    def closed_won(self) -> pd.DataFrame:
        return self.df[self.df["is_closed_won"]].copy()

    @cached_property
    def closed_won_nb(self) -> pd.DataFrame:
        return self.closed_won[self.closed_won["is_new_business"]].copy()

    @cached_property
    def closed_won_exp(self) -> pd.DataFrame:
        return self.closed_won[self.closed_won["is_expansion"]].copy()

    @cached_property
    def closed_lost(self) -> pd.DataFrame:
        return self.df[self.df["is_closed_lost"]].copy()

    @cached_property
    def open_pipeline(self) -> pd.DataFrame:
        return self.df[self.df["is_open"]].copy()

    @cached_property
    def forecastable_pipeline(self) -> pd.DataFrame:
        """Open pipeline excluding 'New - Meeting Set' — used for forecast calculations."""
        return self.open_pipeline[self.open_pipeline["stage"] != "New - Meeting Set"].copy()

    # ------------------------------------------------------------------
    # YTD totals
    # ------------------------------------------------------------------
    @cached_property
    def ytd_closed_won_acv(self) -> float:
        return float(self.closed_won["acv"].sum())

    @cached_property
    def ytd_closed_won_nb_acv(self) -> float:
        return float(self.closed_won_nb["acv"].sum())

    @cached_property
    def ytd_closed_won_exp_acv(self) -> float:
        return float(self.closed_won_exp["acv"].sum())

    @cached_property
    def ytd_closed_won_count(self) -> int:
        return len(self.closed_won)

    @cached_property
    def total_pipeline_acv(self) -> float:
        return float(self.open_pipeline["acv"].sum())

    @cached_property
    def total_weighted_pipeline(self) -> float:
        return float(self.forecastable_pipeline["weighted_acv"].sum())

    @cached_property
    def ytd_target_nb(self) -> float:
        return sum(
            self.cfg.monthly_target(m, "new_biz")
            for m in range(1, self._current_month + 1)
        )

    @cached_property
    def ytd_target_exp(self) -> float:
        return sum(
            self.cfg.monthly_target(m, "expansion")
            for m in range(1, self._current_month + 1)
        )

    @cached_property
    def ytd_attainment_nb(self) -> float:
        if self.ytd_target_nb == 0:
            return 0.0
        return self.ytd_closed_won_nb_acv / self.ytd_target_nb

    @cached_property
    def ytd_attainment_exp(self) -> float:
        if self.ytd_target_exp == 0:
            return 0.0
        return self.ytd_closed_won_exp_acv / self.ytd_target_exp

    @cached_property
    def pipeline_coverage(self) -> float:
        remaining = self.cfg.targets["net_new_arr"] - self.ytd_closed_won_nb_acv
        if remaining <= 0:
            return 999.0
        return self.total_pipeline_acv / remaining

    # ------------------------------------------------------------------
    # Monthly aggregations
    # ------------------------------------------------------------------
    @cached_property
    def monthly_closed_won_nb(self) -> dict[int, float]:
        """ACV closed won per month (new business)."""
        grp = self.closed_won_nb.groupby("close_month")["acv"].sum()
        return {m: float(grp.get(m, 0)) for m in MONTHS}

    @cached_property
    def monthly_closed_won_exp(self) -> dict[int, float]:
        grp = self.closed_won_exp.groupby("close_month")["acv"].sum()
        return {m: float(grp.get(m, 0)) for m in MONTHS}

    @cached_property
    def monthly_closed_won_count(self) -> dict[int, int]:
        grp = self.closed_won.groupby("close_month").size()
        return {m: int(grp.get(m, 0)) for m in MONTHS}

    @cached_property
    def monthly_opps_created(self) -> dict[int, int]:
        grp = self.df.groupby("created_month").size()
        return {m: int(grp.get(m, 0)) for m in MONTHS}

    # ------------------------------------------------------------------
    # AE-level
    # ------------------------------------------------------------------
    def ae_quota(self, name: str) -> float:
        for ae in self.cfg.ae_roster:
            if ae["name"] == name:
                return float(ae.get("quota", 0))
        return 0.0

    def ae_segment(self, name: str) -> str:
        for ae in self.cfg.ae_roster:
            if ae["name"] == name:
                return ae.get("segment", "")
        return ""

    @cached_property
    def ae_closed_won(self) -> dict[str, float]:
        grp = self.closed_won.groupby("owner")["acv"].sum()
        return {name: float(grp.get(name, 0)) for name in self.cfg.ae_names}

    @cached_property
    def ae_closed_won_count(self) -> dict[str, int]:
        grp = self.closed_won.groupby("owner").size()
        return {name: int(grp.get(name, 0)) for name in self.cfg.ae_names}

    @cached_property
    def ae_pipeline(self) -> dict[str, float]:
        grp = self.open_pipeline.groupby("owner")["acv"].sum()
        return {name: float(grp.get(name, 0)) for name in self.cfg.ae_names}

    @cached_property
    def ae_weighted_pipeline(self) -> dict[str, float]:
        grp = self.forecastable_pipeline.groupby("owner")["weighted_acv"].sum()
        return {name: float(grp.get(name, 0)) for name in self.cfg.ae_names}

    @cached_property
    def ae_deal_count(self) -> dict[str, int]:
        grp = self.open_pipeline.groupby("owner").size()
        return {name: int(grp.get(name, 0)) for name in self.cfg.ae_names}

    def ae_monthly_closed(self, name: str) -> dict[int, float]:
        ae_won = self.closed_won[self.closed_won["owner"] == name]
        grp = ae_won.groupby("close_month")["acv"].sum()
        return {m: float(grp.get(m, 0)) for m in MONTHS}

    @cached_property
    def ae_slt_forecast(self) -> dict[str, float]:
        """Per-AE model-driven projection: closed won + weighted pipeline."""
        return {
            name: self.ae_closed_won.get(name, 0) + self.ae_weighted_pipeline.get(name, 0)
            for name in self.cfg.ae_only_names
        }

    # ------------------------------------------------------------------
    # Pipeline by stage
    # ------------------------------------------------------------------
    @cached_property
    def pipeline_by_stage(self) -> list[dict]:
        """Returns list of dicts: stage, count, acv, weighted_acv."""
        result = []
        for stage in self.cfg.stage_names:
            if self.cfg.stage_category(stage) != "Pipeline":
                continue
            stage_df = self.open_pipeline[self.open_pipeline["stage"] == stage]
            result.append({
                "stage": stage,
                "phase": self.cfg.stage_phase(stage),
                "count": len(stage_df),
                "acv": float(stage_df["acv"].sum()),
                "weighted_acv": float(stage_df["weighted_acv"].sum()),
            })
        return result

    @cached_property
    def pipeline_by_segment(self) -> dict[str, dict]:
        result = {}
        for seg in ["SMB", "MM", "Ent"]:
            seg_df = self.open_pipeline[self.open_pipeline["segment"] == seg]
            result[seg] = {
                "count": len(seg_df),
                "acv": float(seg_df["acv"].sum()),
                "weighted_acv": float(seg_df["weighted_acv"].sum()),
            }
        return result

    @cached_property
    def pipeline_by_aging(self) -> dict[str, dict]:
        result = {}
        for bucket in ["0-30", "31-60", "61-90", "90+"]:
            b_df = self.open_pipeline[self.open_pipeline["aging_bucket"] == bucket]
            result[bucket] = {
                "count": len(b_df),
                "acv": float(b_df["acv"].sum()),
            }
        return result

    # ------------------------------------------------------------------
    # Segment analysis
    # ------------------------------------------------------------------
    def segment_deals(self, segment: str, won_only: bool = False) -> pd.DataFrame:
        base = self.closed_won if won_only else self.df
        return base[base["segment"] == segment]

    @cached_property
    def segment_summary(self) -> dict[str, dict]:
        result = {}
        for seg in ["SMB", "MM", "Ent"]:
            won = self.segment_deals(seg, won_only=True)
            all_decided = self.df[
                (self.df["segment"] == seg) &
                (self.df["stage"].isin(["Closed Won", "Closed Lost"]))
            ]
            won_count = len(won)
            total_decided = len(all_decided)
            result[seg] = {
                "deals_won": won_count,
                "revenue": float(won["acv"].sum()),
                "win_rate": won_count / total_decided if total_decided > 0 else 0.0,
                "avg_deal_size": float(won["acv"].mean()) if won_count > 0 else 0.0,
                "target_ads": self.cfg.segment_ads(seg),
                "pipeline_count": len(self.open_pipeline[self.open_pipeline["segment"] == seg]),
                "pipeline_acv": float(
                    self.open_pipeline[self.open_pipeline["segment"] == seg]["acv"].sum()
                ),
            }
        return result

    # ------------------------------------------------------------------
    # Funnel metrics
    # ------------------------------------------------------------------
    @cached_property
    def funnel_by_stage(self) -> dict[str, int]:
        """Count of all opps that reached each stage."""
        return dict(self.df["stage"].value_counts())

    @cached_property
    def funnel_by_lead_source(self) -> dict[str, dict]:
        result = {}
        for src in self.df["lead_source"].unique():
            if str(src) == "nan":
                continue
            src_df = self.df[self.df["lead_source"] == src]
            won_df = src_df[src_df["is_closed_won"]]
            result[src] = {
                "total": len(src_df),
                "won": len(won_df),
                "revenue": float(won_df["acv"].sum()),
                "win_rate": len(won_df) / len(src_df) if len(src_df) > 0 else 0.0,
            }
        return result

    @cached_property
    def no_show_count(self) -> int:
        return len(self.df[self.df["stage"] == "No Show"])

    @cached_property
    def no_show_rate(self) -> float:
        meeting_set = len(self.df[self.df["stage"].isin(["New - Meeting Set", "No Show"])])
        if meeting_set == 0:
            return 0.0
        return self.no_show_count / meeting_set

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------
    @cached_property
    def monthly_expansion_actual(self) -> dict[int, float]:
        grp = self.closed_won_exp.groupby("close_month")["acv"].sum()
        return {m: float(grp.get(m, 0)) for m in MONTHS}

    @cached_property
    def expansion_by_ae(self) -> dict[str, float]:
        grp = self.closed_won_exp.groupby("owner")["acv"].sum()
        return dict(grp)

    @cached_property
    def expansion_by_account(self) -> pd.DataFrame:
        return self.closed_won_exp[
            ["organization", "owner", "opp_name", "acv", "close_date", "close_month"]
        ].sort_values("close_date")

    # ------------------------------------------------------------------
    # SDR metrics
    # ------------------------------------------------------------------
    @cached_property
    def sdr_opps_created(self) -> dict[str, int]:
        sdr_df = self.df[self.df["lead_source"] == "SDR"]
        grp = sdr_df.groupby("created_by").size()
        return dict(grp)

    @cached_property
    def sdr_pipeline_generated(self) -> dict[str, float]:
        sdr_df = self.df[self.df["lead_source"] == "SDR"]
        grp = sdr_df.groupby("created_by")["acv"].sum()
        return {k: float(v) for k, v in grp.items()}

    @cached_property
    def sdr_closed_won_attributed(self) -> dict[str, float]:
        sdr_won = self.closed_won[self.closed_won["lead_source"] == "SDR"]
        grp = sdr_won.groupby("created_by")["acv"].sum()
        return {k: float(v) for k, v in grp.items()}

    @cached_property
    def sdr_monthly_opps(self) -> dict[str, dict[int, int]]:
        sdr_df = self.df[self.df["lead_source"] == "SDR"]
        result = {}
        for name in sdr_df["created_by"].unique():
            person = sdr_df[sdr_df["created_by"] == name]
            grp = person.groupby("created_month").size()
            result[str(name)] = {m: int(grp.get(m, 0)) for m in MONTHS}
        return result

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------
    @cached_property
    def forecast(self) -> dict[int, dict]:
        """Hybrid forecast: actuals → weighted pipeline → run rate."""
        result = {}
        # Compute average monthly run rate from actuals
        actual_months = [
            m for m in MONTHS
            if m < self._current_month and self.monthly_closed_won_nb[m] > 0
        ]
        if actual_months:
            avg_run_rate = sum(self.monthly_closed_won_nb[m] for m in actual_months) / len(actual_months)
        else:
            avg_run_rate = self.cfg.targets["net_new_arr"] / 12

        for m in MONTHS:
            if m < self._current_month:
                # Past months: use actuals
                result[m] = {
                    "method": "Actual",
                    "new_biz": self.monthly_closed_won_nb[m],
                    "expansion": self.monthly_expansion_actual[m],
                }
            elif m <= self._current_month + 1:
                # Current + next month: weighted pipeline (excludes New - Meeting Set)
                month_pipe = self.forecastable_pipeline[self.forecastable_pipeline["close_month"] == m]
                result[m] = {
                    "method": "Weighted Pipeline",
                    "new_biz": float(month_pipe[month_pipe["is_new_business"]]["weighted_acv"].sum()),
                    "expansion": float(month_pipe[month_pipe["is_expansion"]]["weighted_acv"].sum()),
                }
            else:
                # Future: seasonally adjusted run rate
                seasonal = self.cfg.seasonality.get(m, 1.0)
                result[m] = {
                    "method": "Run Rate",
                    "new_biz": avg_run_rate * seasonal,
                    "expansion": self.cfg.monthly_target(m, "expansion") * 0.8,
                }
        return result

    @cached_property
    def forecast_scenarios(self) -> dict[str, float]:
        """Conservative / Base / Optimistic full-year totals."""
        base_total = sum(f["new_biz"] + f["expansion"] for f in self.forecast.values())
        return {
            "Conservative": base_total * 0.85,
            "Base": base_total,
            "Optimistic": base_total * 1.15,
        }

    # ------------------------------------------------------------------
    # ARPL, ADS, LPD (current vs target)
    # ------------------------------------------------------------------
    @cached_property
    def current_arpl(self) -> dict[str, float]:
        """Current ARPL by segment (revenue / locations for closed won deals)."""
        result = {}
        for seg in ["SMB", "MM", "Ent"]:
            won = self.closed_won[self.closed_won["segment"] == seg]
            total_rev = float(won["acv"].sum())
            total_locs = float(won["locations"].sum())
            result[seg] = total_rev / total_locs if total_locs > 0 else 0.0
        # Blended
        total_rev = float(self.closed_won["acv"].sum())
        total_locs = float(self.closed_won["locations"].sum())
        result["Blended"] = total_rev / total_locs if total_locs > 0 else 0.0
        return result

    @cached_property
    def current_ads(self) -> dict[str, float]:
        """Current ADS by segment (avg ACV of closed won deals)."""
        result = {}
        for seg in ["SMB", "MM", "Ent"]:
            won = self.closed_won[self.closed_won["segment"] == seg]
            result[seg] = float(won["acv"].mean()) if len(won) > 0 else 0.0
        result["Blended"] = float(self.closed_won["acv"].mean()) if len(self.closed_won) > 0 else 0.0
        return result

    @cached_property
    def current_lpd(self) -> dict[str, float]:
        """Current avg locations per deal by segment (closed won)."""
        result = {}
        for seg in ["SMB", "MM", "Ent"]:
            won = self.closed_won[self.closed_won["segment"] == seg]
            result[seg] = float(won["locations"].mean()) if len(won) > 0 else 0.0
        result["Blended"] = float(self.closed_won["locations"].mean()) if len(self.closed_won) > 0 else 0.0
        return result

    # ------------------------------------------------------------------
    # Monthly unit economics (ARPL, ADS, LPD by segment per month)
    # ------------------------------------------------------------------
    def monthly_unit_economics(self, month: int) -> dict[str, dict[str, float]]:
        """Unit economics for a specific month.

        Returns dict with keys: ARPL, ADS, LPD.
        Each maps segment (SMB, MM, Ent, Blended) to a value.
        """
        won_m = self.closed_won[self.closed_won["close_month"] == month]
        result = {"ARPL": {}, "ADS": {}, "LPD": {}}

        for seg in ["SMB", "MM", "Ent"]:
            seg_df = won_m[won_m["segment"] == seg]
            rev = float(seg_df["acv"].sum())
            locs = float(seg_df["locations"].sum())
            deals = len(seg_df)

            result["ARPL"][seg] = rev / locs if locs > 0 else 0.0
            result["ADS"][seg] = rev / deals if deals > 0 else 0.0
            result["LPD"][seg] = locs / deals if deals > 0 else 0.0

        # Blended
        rev = float(won_m["acv"].sum())
        locs = float(won_m["locations"].sum())
        deals = len(won_m)
        result["ARPL"]["Blended"] = rev / locs if locs > 0 else 0.0
        result["ADS"]["Blended"] = rev / deals if deals > 0 else 0.0
        result["LPD"]["Blended"] = locs / deals if deals > 0 else 0.0

        return result

    # ------------------------------------------------------------------
    # Quarterly funnel actuals
    # ------------------------------------------------------------------
    def _quarter_months(self, q: str) -> list[int]:
        return {"Q1": [1, 2, 3], "Q2": [4, 5, 6], "Q3": [7, 8, 9], "Q4": [10, 11, 12]}[q]

    @cached_property
    def quarterly_funnel_actual(self) -> dict[str, dict[str, float]]:
        """Actual closed won deal count by quarter and segment."""
        result = {}
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            months = self._quarter_months(q)
            q_result = {}
            for seg in ["SMB", "MM", "Ent"]:
                won = self.closed_won_nb[
                    (self.closed_won_nb["segment"] == seg) &
                    (self.closed_won_nb["close_month"].isin(months))
                ]
                q_result[seg] = float(len(won))
            result[q] = q_result
        return result

    # ------------------------------------------------------------------
    # Win rate (overall)
    # ------------------------------------------------------------------
    @cached_property
    def overall_win_rate(self) -> float:
        decided = self.df[self.df["stage"].isin(["Closed Won", "Closed Lost"])]
        if len(decided) == 0:
            return 0.0
        return len(self.closed_won) / len(decided)

    @cached_property
    def avg_deal_size(self) -> float:
        if len(self.closed_won) == 0:
            return 0.0
        return float(self.closed_won["acv"].mean())

    @cached_property
    def avg_sales_cycle(self) -> float:
        if len(self.closed_won) == 0:
            return 0.0
        return float(self.closed_won["age"].mean())
