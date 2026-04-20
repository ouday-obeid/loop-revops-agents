"""CSV ingestion, cleaning, and enrichment."""

from __future__ import annotations

import sys
import warnings as _warnings
from pathlib import Path

import pandas as pd

from core.config_schema import Config


def _parse_currency(series: pd.Series) -> pd.Series:
    """Strip $, commas, and convert to float. Blanks become 0."""
    return (
        series.astype(str)
        .str.replace("$", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip()
        .replace({"": "0", "nan": "0", "None": "0"})
        .astype(float)
    )


def _parse_date(series: pd.Series) -> pd.Series:
    """Parse dates flexibly, coercing failures to NaT."""
    return pd.to_datetime(series, format="mixed", dayfirst=False, errors="coerce")


def load_csv(path: str | Path, cfg: Config) -> pd.DataFrame:
    """Load and validate a Salesforce CSV export."""
    path = Path(path)
    if not path.exists():
        print(f"ERROR: CSV file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="latin-1")
    df.columns = df.columns.str.strip()

    col_map = cfg.csv_columns
    warn_count = 0

    # Optional columns that don't cause a fatal error if missing
    OPTIONAL_COLS = {"opp_notes", "loss_notes", "contract_type",
                     "count_balance", "count_base_insights", "count_compass",
                     "count_guard", "count_recover", "count_reengage",
                     "count_truroi", "count_truroi_plus", "count_white_glove"}

    # Validate required columns exist
    for internal, csv_col in col_map.items():
        if csv_col not in df.columns:
            if internal in OPTIONAL_COLS:
                print(f"  NOTE: Optional column '{csv_col}' not in CSV, will use blank", file=sys.stderr)
                df[csv_col] = ""
            else:
                print(f"ERROR: CSV missing required column '{csv_col}' (mapped as '{internal}')",
                      file=sys.stderr)
                print(f"  Available columns: {list(df.columns)}", file=sys.stderr)
                sys.exit(1)

    # Rename to internal names
    rename_map = {v: k for k, v in col_map.items()}
    df = df.rename(columns=rename_map)

    # --- Clean & parse ---
    # Strip whitespace from string columns
    str_cols = ["organization", "owner", "opp_name", "brand", "record_type",
                "stage", "segment", "lead_source", "trade_show", "created_by",
                "opp_notes", "loss_notes", "contract_type"]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    # Parse currency fields
    df["acv"] = _parse_currency(df["acv"])
    if "expansion_acv" in df.columns:
        df["expansion_acv"] = _parse_currency(df["expansion_acv"])
    else:
        df["expansion_acv"] = 0.0

    # Parse numeric fields
    if "locations" in df.columns:
        df["locations"] = pd.to_numeric(df["locations"], errors="coerce").fillna(0).astype(int)
    if "age" in df.columns:
        df["age"] = pd.to_numeric(df["age"], errors="coerce").fillna(0).astype(int)

    # Parse product count columns (1/0 flags) and build combined "products" string
    PRODUCT_COLS = {
        "count_balance": "Balance",
        "count_base_insights": "Base/Insights",
        "count_compass": "Compass",
        "count_guard": "Guard",
        "count_recover": "Recover",
        "count_reengage": "Re-engage",
        "count_truroi": "TruROI",
        "count_truroi_plus": "TruROI+",
        "count_white_glove": "White Glove",
    }
    for col in PRODUCT_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    def _build_products(row):
        prods = []
        for col, label in PRODUCT_COLS.items():
            if col in row.index and row[col] >= 1:
                prods.append(label)
        return ", ".join(prods) if prods else ""

    df["products"] = df.apply(_build_products, axis=1)
    df["product_count"] = sum(
        df[col] for col in PRODUCT_COLS if col in df.columns
    )

    # Parse date fields
    df["created_date"] = _parse_date(df["created_date"])
    df["close_date"] = _parse_date(df["close_date"])

    # Warn on date parse failures
    bad_created = df["created_date"].isna().sum()
    bad_close = df["close_date"].isna().sum()
    if bad_created:
        print(f"  WARNING: {bad_created} rows with unparseable Created Date", file=sys.stderr)
        warn_count += 1
    if bad_close:
        print(f"  WARNING: {bad_close} rows with unparseable Close Date", file=sys.stderr)
        warn_count += 1

    # --- Enrich ---
    df = _enrich(df, cfg)

    # Warn on unmapped stages
    unmapped = df[df["stage_category"] == "Unknown"]["stage"].unique()
    if len(unmapped):
        print(f"  WARNING: Unmapped stages: {list(unmapped)}", file=sys.stderr)
        warn_count += 1

    return df


def _enrich(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Add derived columns for analysis."""
    # Close month (1-12)
    df["close_month"] = df["close_date"].dt.month
    df["close_year"] = df["close_date"].dt.year
    df["created_month"] = df["created_date"].dt.month

    # Stage category and phase from config
    df["stage_category"] = df["stage"].map(lambda s: cfg.stage_category(s))
    df["stage_phase"] = df["stage"].map(lambda s: cfg.stage_phase(s))

    # Weighted ACV = ACV * stage win rate
    df["weighted_acv"] = df.apply(
        lambda row: row["acv"] * cfg.stage_win_rate(row["stage_phase"])
        if row["stage_category"] == "Pipeline" else 0.0,
        axis=1,
    )

    # Boolean flags
    df["is_new_business"] = df["record_type"] == "New Business"
    df["is_expansion"] = df["record_type"] == "Expansion"
    df["is_closed_won"] = df["stage"] == "Closed Won"
    df["is_closed_lost"] = df["stage"] == "Closed Lost"
    df["is_open"] = df["stage_category"] == "Pipeline"

    # Aging buckets for open deals
    df["aging_bucket"] = pd.cut(
        df["age"],
        bins=[-1, 30, 60, 90, 9999],
        labels=["0-30", "31-60", "61-90", "90+"],
    )

    # Segment cleanup
    df["segment"] = df["segment"].replace({"Unknown": "SMB"})

    return df
