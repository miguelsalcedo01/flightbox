"""Pre-run cost estimates: what this ADW usually costs, said BEFORE it spends.

The trace already remembers what every finished run cost. An estimate is just
that history read back at launch: median for "what it usually costs", p90 for
"what a bad day costs", worst for the record. No model, no pricing tables —
the factory's own bills are the dataset, which keeps the number honest and
repo-specific for free.

The estimate is advisory; the budget gates (max_run_cost / max_cost) are the
enforcement. The one place they meet: when the p90 already clears the run's
cap, the launch banner says so — the engineer learns "this will probably halt
on its budget" before the first token is bought, not from the halt itself.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from .data_types import EventRecord


class CostEstimate(BaseModel):
    adw_name: str
    runs: int                       # finished paid runs backing these numbers
    median_cost: float = 0.0
    p90_cost: float = 0.0
    worst_cost: float = 0.0


def for_adw(tracer, adw_name: str) -> CostEstimate:
    """Summarize the finished runs of exactly this ADW. runs == 0 means the
    history is silent — a first run has no estimate, and saying so beats
    inventing one."""
    costs = sorted(tracer.session_cost_history(adw_name))
    if not costs:
        return CostEstimate(adw_name=adw_name, runs=0)
    return CostEstimate(
        adw_name=adw_name, runs=len(costs),
        median_cost=_percentile(costs, 0.5),
        p90_cost=_percentile(costs, 0.9),
        worst_cost=costs[-1])


def announce(run, adw_name: str) -> CostEstimate:
    """Compute, trace, and print the estimate at launch.

    Always traced (an `event` row named cost_estimate, empty phase_id — it
    belongs to the run, not to any phase). Printed only when there is history
    to stand on; a warning line is added when p90 clears max_run_cost.
    """
    est = for_adw(run.tracer, adw_name)
    cap = run.cfg.defaults.max_run_cost
    likely_halt = (est.runs > 0 and cap is not None and est.p90_cost > cap)
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, type="log", name="cost_estimate",
        payload={**est.model_dump(), "max_run_cost": cap,
                 "likely_halt": likely_halt}))
    if est.runs:
        run.console.note(
            f"cost estimate ({adw_name}, {est.runs} prior run"
            f"{'s' if est.runs != 1 else ''}): median ${est.median_cost:.4f}"
            f" · p90 ${est.p90_cost:.4f} · worst ${est.worst_cost:.4f}")
    if likely_halt:
        run.console.note(
            f"WARNING: p90 ${est.p90_cost:.4f} exceeds max_run_cost ${cap:.4f}"
            f" — this run will likely halt on its budget cap")
    return est


def _percentile(ascending: list[float], q: float) -> float:
    """Nearest-rank percentile — no interpolation, every answer is a cost that
    actually happened."""
    rank = max(1, math.ceil(q * len(ascending)))
    return ascending[rank - 1]
