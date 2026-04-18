"""YAML configuration loader and validator."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


REQUIRED_TOP_KEYS = [
    "fiscal_year",
    "targets",
    "monthly_targets",
    "segments",
    "rates",
    "seasonality",
    "stage_mapping",
    "ae_roster",
    "csv_columns",
]

REQUIRED_TARGET_KEYS = ["gross_new_arr", "net_new_arr", "expansion_arr", "churn_rate"]
REQUIRED_CSV_COLS = [
    "organization", "owner", "record_type", "stage", "acv",
    "segment", "created_date", "close_date",
]


class Config:
    """Holds all configuration values with attribute access."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        # Convenient top-level attributes
        self.fiscal_year: int = data["fiscal_year"]
        self.targets: dict = data["targets"]
        self.monthly_targets: dict[int, dict] = {
            int(k): v for k, v in data["monthly_targets"].items()
        }
        self.segments: dict = data["segments"]
        self.rates: dict = data["rates"]
        self.seasonality: dict[int, float] = {
            int(k): float(v) for k, v in data["seasonality"].items()
        }
        self.stage_mapping: dict[str, dict] = data["stage_mapping"]
        self.ae_roster: list[dict] = data.get("ae_roster", [])
        self.sdr_roster: list[dict] = data.get("sdr_roster", [])
        self.headcount: dict = data.get("headcount", {})
        self.csv_columns: dict[str, str] = data["csv_columns"]
        self.blended_targets: dict = data.get("blended_targets", {})
        self.quarterly_funnel_targets: dict = data.get("quarterly_funnel_targets", {})
        self.manager_groups: dict[str, list[str]] = data.get("manager_groups", {})

    # Convenience helpers
    @property
    def ae_names(self) -> list[str]:
        return [ae["name"] for ae in self.ae_roster]

    @property
    def ae_only_roster(self) -> list[dict]:
        """AEs only - excludes Managers, SDRs, and other non-AE roles."""
        return [ae for ae in self.ae_roster if ae.get("role", "AE") == "AE"]

    @property
    def ae_only_names(self) -> list[str]:
        return [ae["name"] for ae in self.ae_only_roster]

    @property
    def sdr_names(self) -> list[str]:
        return [s["name"] for s in self.sdr_roster]

    @property
    def stage_names(self) -> list[str]:
        return list(self.stage_mapping.keys())

    def stage_category(self, stage: str) -> str:
        mapping = self.stage_mapping.get(stage)
        if mapping:
            return mapping.get("category", "Unknown")
        return "Unknown"

    def stage_phase(self, stage: str) -> str | None:
        mapping = self.stage_mapping.get(stage)
        if mapping:
            return mapping.get("phase")
        return None

    def stage_win_rate(self, phase: str | None) -> float:
        if phase is None:
            return 0.0
        return self.rates.get("stage_win_rates", {}).get(phase, 0.0)

    def monthly_target(self, month: int, kind: str = "new_biz") -> float:
        mt = self.monthly_targets.get(month, {})
        return float(mt.get(kind, 0))

    def segment_ads(self, segment: str) -> float:
        seg = self.segments.get(segment, {})
        return float(seg.get("avg_deal_size", 0))

    def segment_target_ads(self, segment: str) -> float:
        seg = self.segments.get(segment, {})
        return float(seg.get("target_ads", seg.get("avg_deal_size", 0)))

    def segment_target_arpl(self, segment: str) -> float:
        seg = self.segments.get(segment, {})
        return float(seg.get("target_arpl", 0))

    def segment_target_lpd(self, segment: str) -> float:
        seg = self.segments.get(segment, {})
        return float(seg.get("target_lpd", 0))

    def quarterly_funnel_target(self, quarter: str, segment: str) -> float:
        qt = self.quarterly_funnel_targets.get(quarter, {})
        return float(qt.get(segment, 0))

    def manager_for_ae(self, name: str) -> str:
        """Return the manager name for a given AE, or 'Unassigned'."""
        for mgr, members in self.manager_groups.items():
            if name in members:
                return mgr
        return "Unassigned"

    def manager_for_ae(self, name: str) -> str:
        """Return the manager name for a given AE, or 'Unassigned'."""
        for mgr, members in self.manager_groups.items():
            if name in members:
                return mgr
        return "Unassigned"


def load_config(path: str | Path) -> Config:
    """Load and validate config from a YAML file."""
    path = Path(path)
    if not path.exists():
        print(f"ERROR: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        print("ERROR: Config file must be a YAML mapping.", file=sys.stderr)
        sys.exit(1)

    warnings: list[str] = []

    # Check required top-level keys
    for key in REQUIRED_TOP_KEYS:
        if key not in data:
            print(f"ERROR: Missing required config key: '{key}'", file=sys.stderr)
            sys.exit(1)

    # Check target keys
    for key in REQUIRED_TARGET_KEYS:
        if key not in data["targets"]:
            print(f"ERROR: Missing target key: 'targets.{key}'", file=sys.stderr)
            sys.exit(1)

    # Check CSV column mappings
    for key in REQUIRED_CSV_COLS:
        if key not in data["csv_columns"]:
            print(f"ERROR: Missing CSV column mapping: 'csv_columns.{key}'", file=sys.stderr)
            sys.exit(1)

    # Check monthly targets (should have 12 months)
    months = {int(k) for k in data["monthly_targets"].keys()}
    missing_months = set(range(1, 13)) - months
    if missing_months:
        warnings.append(f"Monthly targets missing for months: {sorted(missing_months)}")

    # Check seasonality (should have 12 months)
    season_months = {int(k) for k in data["seasonality"].keys()}
    missing_season = set(range(1, 13)) - season_months
    if missing_season:
        warnings.append(f"Seasonality index missing for months: {sorted(missing_season)}")

    # Print warnings
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)

    return Config(data)
