"""Match forecast doc deal names to Salesforce opportunity organizations."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from core.forecast_loader import RepDeal, RepForecastData


@dataclass
class MatchedDeal:
    """A forecast deal with its SF match (if found)."""
    forecast_deal: RepDeal
    sf_org: str = ""
    sf_stage: str = ""
    sf_acv: float = 0.0
    sf_opp_name: str = ""
    matched: bool = False
    acv_delta: float = 0.0
    sf_close_date: str = ""
    sf_locations: int = 0
    sf_products: str = ""
    sf_contract_type: str = ""
    sf_opp_notes: str = ""


def _normalize(name: str) -> str:
    """Normalize a name for matching: lowercase, strip punctuation,
    remove common suffixes and parentheticals."""
    s = name.lower().strip()
    # Remove parentheticals like "(mcdy)"
    s = re.sub(r"\([^)]*\)", "", s)
    # Remove punctuation: apostrophes, question marks, periods, commas
    s = re.sub(r"[?'\".,!]", "", s)
    # Remove common suffixes
    for suffix in ("llc", "inc", "corp", "ltd", "co", "company"):
        s = re.sub(rf"\b{suffix}\b\.?", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _prefix_match(a: str, b: str, min_words: int = 2) -> bool:
    """Check if the first N words of a match the first N words of b."""
    words_a = a.split()
    words_b = b.split()
    if len(words_a) < min_words or len(words_b) < min_words:
        return False
    return words_a[:min_words] == words_b[:min_words]


def match_deals(
    forecast_data: RepForecastData,
    df: pd.DataFrame,
    verbose: bool = False,
) -> dict[str, list[MatchedDeal]]:
    """Match forecast deals to SF opportunities, scoped per rep.

    Returns a dict mapping rep full_name -> list of MatchedDeal.
    """
    results: dict[str, list[MatchedDeal]] = {}
    total_deals = 0
    total_matched = 0

    for rep_name, rep in forecast_data.reps.items():
        if not rep.deals:
            continue

        # Get SF opportunities for this rep
        rep_opps = df[df["owner"] == rep_name].copy()
        if rep_opps.empty:
            # All deals unmatched
            results[rep_name] = [
                MatchedDeal(forecast_deal=d) for d in rep.deals
            ]
            total_deals += len(rep.deals)
            continue

        # Build normalized lookup for SF orgs
        sf_records: list[dict] = []
        for _, row in rep_opps.iterrows():
            org = str(row.get("organization", ""))
            sf_records.append({
                "org": org,
                "org_norm": _normalize(org),
                "stage": str(row.get("stage", "")),
                "acv": float(row.get("acv", 0)),
                "opp_name": str(row.get("opp_name", "")),
                "close_date": row.get("close_date", "").strftime("%m/%d/%Y") if hasattr(row.get("close_date", ""), "strftime") else str(row.get("close_date", "")),
                "locations": int(row.get("locations", 0)) if str(row.get("locations", 0)) != "nan" else 0,
                "products": str(row.get("products", "")),
                "contract_type": str(row.get("contract_type", "")),
                "opp_notes": str(row.get("opp_notes", "")),
            })

        matched_deals: list[MatchedDeal] = []
        used_sf_indices: set[int] = set()

        def _apply_match(md: MatchedDeal, sf: dict, idx: int):
            """Apply SF match data to a MatchedDeal."""
            md.sf_org = sf["org"]
            md.sf_stage = sf["stage"]
            md.sf_acv = sf["acv"]
            md.sf_opp_name = sf["opp_name"]
            md.sf_close_date = sf["close_date"]
            md.sf_locations = sf["locations"]
            md.sf_products = sf["products"]
            md.sf_contract_type = sf["contract_type"]
            md.sf_opp_notes = sf["opp_notes"]
            md.matched = True
            md.acv_delta = md.forecast_deal.acv - sf["acv"]
            used_sf_indices.add(idx)

        for deal in rep.deals:
            total_deals += 1
            deal_norm = _normalize(deal.name)
            md = MatchedDeal(forecast_deal=deal)

            # Pass 1: Exact match on normalized name
            exact_matches = [
                (i, sf) for i, sf in enumerate(sf_records)
                if i not in used_sf_indices and sf["org_norm"] == deal_norm
            ]

            if len(exact_matches) == 1:
                idx, sf = exact_matches[0]
                _apply_match(md, sf, idx)
                total_matched += 1
                matched_deals.append(md)
                continue

            if len(exact_matches) > 1:
                best_idx, best_sf = min(
                    exact_matches, key=lambda x: abs(deal.acv - x[1]["acv"])
                )
                _apply_match(md, best_sf, best_idx)
                total_matched += 1
                matched_deals.append(md)
                continue

            # Pass 2: Prefix match (first 2+ words)
            prefix_matches = [
                (i, sf) for i, sf in enumerate(sf_records)
                if i not in used_sf_indices and _prefix_match(deal_norm, sf["org_norm"])
            ]

            if len(prefix_matches) == 1:
                idx, sf = prefix_matches[0]
                _apply_match(md, sf, idx)
                total_matched += 1
                matched_deals.append(md)
                continue

            if len(prefix_matches) > 1:
                best_idx, best_sf = min(
                    prefix_matches, key=lambda x: abs(deal.acv - x[1]["acv"])
                )
                _apply_match(md, best_sf, best_idx)
                total_matched += 1
                matched_deals.append(md)
                continue

            # Unmatched
            if verbose:
                print(f"  WARNING: Unmatched forecast deal '{deal.name}' for {rep_name}")
            matched_deals.append(md)

        results[rep_name] = matched_deals

    if verbose and total_deals > 0:
        print(f"\n  Matched {total_matched}/{total_deals} forecast deals to SF")

    return results
