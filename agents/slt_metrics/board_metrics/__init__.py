"""Board-level metrics — ARR, NRR, pipeline coverage, unit economics.

Every module here is pure-function: callers pull SF / Loop Pulse data once
and pipe it through. The Excel builder + briefings read from the emitted
dataclasses (`UnitEconomics`, `BoardMetrics`) — no recomputation downstream.
"""
