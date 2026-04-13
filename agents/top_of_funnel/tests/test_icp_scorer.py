"""D2 unit tests for icp_scorer.score_account.

Covers every dimension, each band, caps, bonus, tiering, and explanation.
Backtest against O's closed_won_sample.json lives in test_icp_backtest.py.
"""
from __future__ import annotations

import pytest

from agents.top_of_funnel.icp_scorer import ICPScore, score_account, score_domain


# ----------------------------------------------------------------- helpers


def _perfect_account() -> dict:
    return {
        "domain": "flynnrg.example.com",
        "ownership_type": "franchise_group_multi_brand",
        "location_count": 400,
        "brand_vertical": "QSR",
        "growth_signals": {
            "recent_ma_activity": True,
            "funding_round_last_12mo": True,
            "new_openings_velocity": True,
        },
        "tech_stack": ["pos_toast", "modern_loyalty_platform", "delivery_platform_integrated"],
        "current_loop_products": ["brand_alpha"],
    }


# ------------------------------------------------------------------- ownership


def test_ownership_franchise_group_multi_brand():
    acc = {"ownership_type": "franchise_group_multi_brand"}
    assert score_account(acc).signals["ownership"] == 25


def test_ownership_franchise_group_single_brand():
    acc = {"ownership_type": "franchise_group_single_brand"}
    assert score_account(acc).signals["ownership"] == 18


def test_ownership_single_brand_franchisee():
    acc = {"ownership_type": "single_brand_franchisee"}
    assert score_account(acc).signals["ownership"] == 10


def test_ownership_independent_scores_zero():
    acc = {"ownership_type": "independent"}
    assert score_account(acc).signals["ownership"] == 0


def test_ownership_missing_defaults_zero():
    assert score_account({}).signals["ownership"] == 0


# --------------------------------------------------------------- location count


@pytest.mark.parametrize(
    "count,expected",
    [
        (500, 25),
        (75, 25),
        (50, 25),
        (49, 18),
        (10, 18),
        (9, 10),
        (3, 10),
        (2, 3),
        (1, 3),
        (0, 3),  # min=0 band matches
    ],
)
def test_location_count_bands(count: int, expected: int):
    result = score_account({"location_count": count})
    assert result.signals["location_count"] == expected


def test_location_count_missing_defaults_zero():
    assert score_account({}).signals["location_count"] == 0


def test_location_count_non_numeric_defaults_zero():
    assert score_account({"location_count": "unknown"}).signals["location_count"] == 0


# ---------------------------------------------------------------- brand vertical


@pytest.mark.parametrize(
    "vertical,expected",
    [("QSR", 20), ("Fast_Casual", 15), ("Casual_Dining", 8), ("Fine_Dining", 3), ("Other", 0)],
)
def test_brand_vertical_bands(vertical: str, expected: int):
    assert score_account({"brand_vertical": vertical}).signals["brand_vertical"] == expected


def test_brand_vertical_unknown_scores_zero():
    assert score_account({"brand_vertical": "Cloud_Kitchen_Dark"}).signals["brand_vertical"] == 0


# ---------------------------------------------------------------- growth signals


def test_growth_signals_all_true_caps_at_max():
    acc = {
        "growth_signals": {
            "recent_ma_activity": True,  # 10
            "funding_round_last_12mo": True,  # 6
            "new_openings_velocity": True,  # 4  →  20 total
        }
    }
    assert score_account(acc).signals["growth_signals"] == 20


def test_growth_signals_partial():
    acc = {"growth_signals": {"recent_ma_activity": True, "new_openings_velocity": True}}
    assert score_account(acc).signals["growth_signals"] == 14


def test_growth_signals_false_values_score_zero():
    acc = {
        "growth_signals": {
            "recent_ma_activity": False,
            "funding_round_last_12mo": None,
        }
    }
    assert score_account(acc).signals["growth_signals"] == 0


def test_growth_signals_missing_dict_scores_zero():
    assert score_account({}).signals["growth_signals"] == 0


# ------------------------------------------------------------------ tech-stack


def test_tech_stack_list_form():
    acc = {"tech_stack": ["pos_toast", "modern_loyalty_platform"]}
    # 5 + 3 = 8
    assert score_account(acc).signals["tech_stack_fit"] == 8


def test_tech_stack_dict_form_uses_truthy_keys():
    acc = {
        "tech_stack": {
            "pos_toast": True,
            "pos_square": True,  # 5 + 5 = 10, hits cap
            "modern_loyalty_platform": True,  # +3, capped at 10
        }
    }
    assert score_account(acc).signals["tech_stack_fit"] == 10


def test_tech_stack_caps_at_max():
    acc = {
        "tech_stack": [
            "pos_toast",  # 5
            "pos_square",  # 5
            "modern_loyalty_platform",  # 3
            "delivery_platform_integrated",  # 2
            "pos_other_integrated",  # 3
        ]
    }
    assert score_account(acc).signals["tech_stack_fit"] == 10


def test_tech_stack_unknown_keys_ignored():
    acc = {"tech_stack": ["carrier_pigeon_integration"]}
    assert score_account(acc).signals["tech_stack_fit"] == 0


# ----------------------------------------------------------------- product attach


def test_product_attach_adds_five_when_nonempty():
    acc = {"current_loop_products": ["brand_alpha"]}
    assert score_account(acc).signals["product_attach"] == 5


def test_product_attach_zero_when_empty():
    assert score_account({"current_loop_products": []}).signals["product_attach"] == 0


def test_product_attach_zero_when_missing():
    assert score_account({}).signals["product_attach"] == 0


def test_product_attach_breaks_tie_against_cold_account():
    """A Loop customer expanding to another brand should outrank a cold account
    of equal firmographic score. This is the whole point of the +5 bonus."""
    cold = {
        "ownership_type": "franchise_group_multi_brand",  # 25
        "location_count": 49,  # 18
        "brand_vertical": "Fast_Casual",  # 15
        "growth_signals": {"new_openings_velocity": True},  # 4
        "tech_stack": ["modern_loyalty_platform"],  # 3 → total 65
    }
    warm = {**cold, "current_loop_products": ["brand_alpha"]}  # 65 + 5 = 70
    assert score_account(cold).total == 65
    assert score_account(warm).total == 70
    assert score_account(warm).total > score_account(cold).total


# -------------------------------------------------------------------- total + tiers


def test_perfect_account_scores_105_tier_a():
    result = score_account(_perfect_account())
    assert result.total == 25 + 25 + 20 + 20 + 10 + 5
    assert result.total == 105
    assert result.tier == "A"


def test_empty_account_scores_zero_tier_d():
    result = score_account({})
    assert result.total == 0
    assert result.tier == "D"


def test_tier_a_boundary_70():
    """A-tier starts at exactly 70 (inclusive)."""
    acc = {
        "ownership_type": "franchise_group_multi_brand",  # 25
        "location_count": 50,  # 25
        "brand_vertical": "Fine_Dining",  # 3
        "growth_signals": {"funding_round_last_12mo": True, "new_openings_velocity": True},  # 10
        "tech_stack": ["delivery_platform_integrated"],  # 2 → 65 — doesn't hit A
    }
    r65 = score_account(acc)
    assert r65.total == 65
    assert r65.tier == "B"

    acc_70 = {**acc, "growth_signals": {"recent_ma_activity": True, "funding_round_last_12mo": True}}  # 16 growth
    r70 = score_account(acc_70)
    # 25+25+3+16+2 = 71 → still A
    assert r70.tier == "A"


def test_tier_b_range_45_to_69():
    acc = {
        "ownership_type": "franchise_group_single_brand",  # 18
        "location_count": 10,  # 18
        "brand_vertical": "Casual_Dining",  # 8
        "growth_signals": {"new_openings_velocity": True},  # 4 → total 48
    }
    r = score_account(acc)
    assert r.total == 48
    assert r.tier == "B"


def test_tier_c_range_25_to_44():
    acc = {
        "ownership_type": "single_brand_franchisee",  # 10
        "location_count": 10,  # 18 → 28
    }
    r = score_account(acc)
    assert r.total == 28
    assert r.tier == "C"


def test_tier_d_below_25():
    acc = {"ownership_type": "independent", "location_count": 2}  # 0 + 3 = 3
    r = score_account(acc)
    assert r.total == 3
    assert r.tier == "D"


# ----------------------------------------------------------------- explanation


def test_explanation_contains_domain_and_total():
    acc = _perfect_account()
    r = score_account(acc)
    assert "flynnrg.example.com" in r.explanation
    assert "105" in r.explanation
    assert "tier A" in r.explanation


def test_explanation_lists_every_dimension():
    r = score_account(_perfect_account())
    for needle in ("Ownership", "Locations", "Vertical", "Growth signals", "Tech-stack fit"):
        assert needle in r.explanation


def test_explanation_omits_bonus_when_not_earned():
    r = score_account({"ownership_type": "independent"})
    assert "Product-attach" not in r.explanation


def test_explanation_shows_bonus_when_earned():
    r = score_account(_perfect_account())
    assert "Product-attach bonus" in r.explanation
    assert "+5" in r.explanation


# ----------------------------------------------------------------- data hygiene


def test_score_account_returns_frozen_dataclass():
    r = score_account({})
    assert isinstance(r, ICPScore)
    with pytest.raises(Exception):  # FrozenInstanceError
        r.total = 99  # type: ignore[misc]


def test_to_dict_roundtrip():
    r = score_account(_perfect_account())
    d = r.to_dict()
    assert d["total"] == r.total
    assert d["tier"] == r.tier
    assert d["signals"]["ownership"] == 25
    assert "explanation" in d


# ----------------------------------------------------------------- slack entry


@pytest.mark.asyncio
async def test_score_domain_unknown_returns_nudge():
    result = await score_domain("nonexistent-domain-xyz.test")
    assert "enrich" in result["text"].lower()


@pytest.mark.asyncio
async def test_score_domain_hits_cache_when_present(tmp_path, monkeypatch):
    """If the pipeline has already written a tof_lead_candidates row, score_domain
    returns a score without needing Apollo."""
    import json
    from sqlalchemy import text
    from agents.top_of_funnel.state import get_state_engine

    engine = get_state_engine()
    account = {
        "ownership_type": "franchise_group_multi_brand",
        "location_count": 120,
        "brand_vertical": "QSR",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_lead_candidates
                       (domain, account_payload, status, created_at)
                       VALUES (:d, :p, 'ready', CURRENT_TIMESTAMP)"""
            ),
            {"d": "scored.example.com", "p": json.dumps(account)},
        )

    result = await score_domain("scored.example.com")
    assert "ICP" in result["text"]
    assert "scored.example.com" in result["text"]
    assert result["score"]["tier"] in {"A", "B"}
