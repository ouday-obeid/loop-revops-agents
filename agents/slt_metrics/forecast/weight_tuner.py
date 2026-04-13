"""Weight tuner — surface O-reviewable proposals, never auto-promote.

Samples ~300 weight combos near the seed (±0.05 per pillar, renormalized to
sum=1), runs each through a backtest, scores on a composite of MAPE + Brier,
and returns the top-K by composite score ascending. O promotes one via
`@oo slt weights set` (governance gate); this module never writes weights
itself.

The backtest runner is injected as a callable so tests can fake it without
bootstrapping a full SF history — and so the dispatcher can plug in a cached
change-log extractor when the same 2-quarter window is reused across 300
samples.
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from agents.slt_metrics.forecast.backtest import BacktestResult
from agents.slt_metrics.forecast.weights import bump_version
from agents.slt_metrics.pipeline.config import WEIGHT_SEEDS
from agents.slt_metrics.types import ForecastWeights

log = logging.getLogger(__name__)


DEFAULT_N_SAMPLES: Final[int] = 300
DEFAULT_MAX_DEVIATION: Final[float] = 0.05
DEFAULT_TOP_K: Final[int] = 3
MIN_PILLAR_WEIGHT: Final[float] = 0.02         # guardrail: don't zero a pillar
COMPOSITE_MAPE_WEIGHT: Final[float] = 0.5
COMPOSITE_BRIER_WEIGHT: Final[float] = 0.5
COMPOSITE_PENALTY: Final[float] = 1.5          # assigned when MAPE or Brier is None


@dataclass
class WeightProposal:
    weights: ForecastWeights
    backtest_result: BacktestResult
    composite_score: float
    rank: int = 0

    @property
    def is_seed(self) -> bool:
        return self.weights.version == WEIGHT_SEEDS.version


def propose_weight_tunings(
    *,
    seed: ForecastWeights | None = None,
    backtest_fn: Callable[[ForecastWeights], BacktestResult],
    n_samples: int = DEFAULT_N_SAMPLES,
    max_deviation: float = DEFAULT_MAX_DEVIATION,
    top_k: int = DEFAULT_TOP_K,
    rng: random.Random | None = None,
    label: str = "tuned",
) -> list[WeightProposal]:
    """Sample N weight combos, backtest each, return the top-K.

    Always includes the seed as one of the scored candidates so O can see
    how the proposals compare to the currently-active weights. The seed is
    kept at its original version string; samples get a `v{n}-{label}-<date>`
    version so O can tell them apart at a glance.
    """
    if n_samples < 1:
        raise ValueError("n_samples must be >= 1")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    if not (0.0 < max_deviation <= 0.2):
        raise ValueError("max_deviation must be in (0, 0.2]")

    seed = seed or WEIGHT_SEEDS
    rng = rng or random.Random()

    candidates: list[ForecastWeights] = [seed]
    for _ in range(n_samples):
        sampled = _sample_near(seed, max_deviation=max_deviation, rng=rng)
        candidates.append(bump_version(sampled, label))

    proposals: list[WeightProposal] = []
    for weights in candidates:
        try:
            result = backtest_fn(weights)
        except Exception:
            log.exception("weight_tuner: backtest_fn raised for %s", weights.version)
            continue
        proposals.append(
            WeightProposal(
                weights=weights,
                backtest_result=result,
                composite_score=_composite_score(result),
            )
        )

    proposals.sort(key=lambda p: p.composite_score)
    top = proposals[:top_k]
    for idx, p in enumerate(top, start=1):
        p.rank = idx
    return top


# ------------------------------------------------------------------ sampling

def _sample_near(
    seed: ForecastWeights,
    *,
    max_deviation: float,
    rng: random.Random,
) -> ForecastWeights:
    """Uniform-perturb each pillar by ±max_deviation, floor, then renormalize."""
    perturbed = {
        "icp":      max(MIN_PILLAR_WEIGHT, seed.icp      + rng.uniform(-max_deviation, max_deviation)),
        "stage":    max(MIN_PILLAR_WEIGHT, seed.stage    + rng.uniform(-max_deviation, max_deviation)),
        "activity": max(MIN_PILLAR_WEIGHT, seed.activity + rng.uniform(-max_deviation, max_deviation)),
        "timeline": max(MIN_PILLAR_WEIGHT, seed.timeline + rng.uniform(-max_deviation, max_deviation)),
        "call":     max(MIN_PILLAR_WEIGHT, seed.call     + rng.uniform(-max_deviation, max_deviation)),
    }
    total = sum(perturbed.values())
    if total <= 0:
        return seed  # degenerate; fall back to seed
    normalized = {k: v / total for k, v in perturbed.items()}
    return ForecastWeights(
        icp=normalized["icp"],
        stage=normalized["stage"],
        activity=normalized["activity"],
        timeline=normalized["timeline"],
        call=normalized["call"],
        version=seed.version,  # bump_version called by caller
    )


# ------------------------------------------------------------------ scoring

def _composite_score(result: BacktestResult) -> float:
    """Lower = better. None MAPE or Brier earns a heavy penalty so the proposal
    still ranks — but ranks behind any candidate that produced numbers."""
    mape = result.overall_mape if result.overall_mape is not None else COMPOSITE_PENALTY
    brier = result.brier_score if result.brier_score is not None else COMPOSITE_PENALTY
    return COMPOSITE_MAPE_WEIGHT * mape + COMPOSITE_BRIER_WEIGHT * brier


# ------------------------------------------------------------------ render

def render_proposals_markdown(proposals: list[WeightProposal]) -> str:
    """Markdown table for `@oo slt weights propose` Slack reply."""
    if not proposals:
        return "_No viable proposals — check backtest window + change logs._"
    lines = [
        "| Rank | Version | ICP | Stage | Activity | Timeline | Call | MAPE | Brier | Score |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in proposals:
        w = p.weights
        r = p.backtest_result
        lines.append(
            f"| {p.rank} | {w.version} | {w.icp:.2f} | {w.stage:.2f} | "
            f"{w.activity:.2f} | {w.timeline:.2f} | {w.call:.2f} | "
            f"{_fmt_pct(r.overall_mape)} | {_fmt_float(r.brier_score)} | "
            f"{p.composite_score:.4f} |"
        )
    return "\n".join(lines)


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_float(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"
