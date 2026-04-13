"""Unit economics — gap-flag vs populated card."""
from __future__ import annotations

from agents.slt_metrics.board_metrics import unit_economics


def test_none_row_returns_gap_flagged_card():
    card = unit_economics.build_unit_economics(None)
    assert card.gap_flag is True
    assert card.net_revenue_retention is None
    assert card.logo_retention is None
    assert card.cac_payback_months is None


def test_empty_mapping_returns_gap_flagged_card():
    card = unit_economics.build_unit_economics({})
    assert card.gap_flag is True


def test_full_row_populates_every_field():
    card = unit_economics.build_unit_economics({
        "gross_revenue_retention": 0.95,
        "net_revenue_retention": 1.12,
        "logo_retention": 0.90,
        "expansion_rate": 0.25,
        "cac_payback_months": 14.5,
        "ltv_cac_ratio": 4.2,
    })
    assert card.gap_flag is False
    assert card.gross_revenue_retention == 0.95
    assert card.net_revenue_retention == 1.12
    assert card.logo_retention == 0.90
    assert card.expansion_rate == 0.25
    assert card.cac_payback_months == 14.5
    assert card.ltv_cac_ratio == 4.2


def test_partial_row_keeps_known_fields_nulls_the_rest():
    card = unit_economics.build_unit_economics({
        "net_revenue_retention": 1.05,
        "logo_retention": None,
    })
    assert card.gap_flag is False
    assert card.net_revenue_retention == 1.05
    assert card.logo_retention is None
    assert card.gross_revenue_retention is None
    assert card.cac_payback_months is None


def test_non_numeric_values_coerce_to_none():
    card = unit_economics.build_unit_economics({
        "net_revenue_retention": "not-a-number",
        "cac_payback_months": "forever",
    })
    # Row was non-empty so gap_flag stays False, but the bad cells bottom out at None.
    assert card.gap_flag is False
    assert card.net_revenue_retention is None
    assert card.cac_payback_months is None


def test_numeric_strings_coerce_to_floats():
    card = unit_economics.build_unit_economics({
        "net_revenue_retention": "1.10",
        "ltv_cac_ratio": "3.5",
    })
    assert card.net_revenue_retention == 1.10
    assert card.ltv_cac_ratio == 3.5
