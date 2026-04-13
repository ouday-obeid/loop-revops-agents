"""Forecast scorer — 5 pillars, probability/category bands, commit/best rollups.

ForecastWeights live in `agents.slt_metrics.types` (shared dataclass); this
package owns *behavior* — how each pillar reads an OppRecord, how weights
persist through forecast_history, and how the composite score maps to
commit/best-case/weighted.
"""
