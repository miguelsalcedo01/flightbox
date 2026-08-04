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

import calendar
import datetime
import math
from typing import Optional

from pydantic import BaseModel

from .data_types import EventRecord


class MonthStatus(BaseModel):
    """Where the month stands, and where today's pace lands it.

    `daily_pace` is the last 7 days averaged — recent behavior, not the
    month's, so a quiet first week does not hide a loud third one. The
    projection is that pace ridden to month-end on top of what is already
    spent. Advisory by design: the month reports, the run caps halt.
    """

    month: str                      # "2026-08"
    spent: float                    # month-to-date, running sessions included
    budget: float
    fraction: float                 # spent / budget
    daily_pace: float               # last-7-days dollars / 7
    days_remaining: int             # full days left after today
    projected: float                # spent + daily_pace * days_remaining
    likely_over: bool               # projected > budget


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


def month_status(tracer, budget: float,
                 today: Optional[datetime.date] = None) -> MonthStatus:
    """Compute the month's standing. `today` is injectable for tests."""
    today = today or datetime.date.today()
    month = today.strftime("%Y-%m")
    spent = tracer.month_cost(month)
    days_remaining = calendar.monthrange(today.year, today.month)[1] - today.day
    pace = tracer.recent_cost(7) / 7.0
    projected = spent + pace * days_remaining
    return MonthStatus(
        month=month, spent=spent, budget=budget,
        fraction=(spent / budget) if budget else 0.0,
        daily_pace=pace, days_remaining=days_remaining,
        projected=projected, likely_over=projected > budget)


def announce_month(run, today: Optional[datetime.date] = None) -> Optional[MonthStatus]:
    """Report the month at launch — only when a month_budget is configured,
    because a projection against no budget is just a number with no line."""
    budget = run.cfg.defaults.month_budget
    if not budget:
        return None
    ms = month_status(run.tracer, budget, today)
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, type="log", name="month_budget",
        payload=ms.model_dump()))
    run.console.note(
        f"month budget ({ms.month}): ${ms.spent:.2f} of ${ms.budget:.2f}"
        f" spent ({min(ms.fraction, 9.99):.0%}) · pace ${ms.daily_pace:.2f}/day"
        f" · projected month-end ${ms.projected:.2f}")
    if ms.fraction >= 1.0:
        run.console.note(f"WARNING: the month budget is exhausted"
                         f" — ${ms.spent - ms.budget:.2f} over")
    elif ms.fraction >= 0.75:
        run.console.note(f"WARNING: {ms.fraction:.0%} of the month budget is spent"
                         f" with {ms.days_remaining} day(s) still to go")
    elif ms.likely_over:
        run.console.note(
            f"WARNING: at the last 7 days' pace this month ends at"
            f" ${ms.projected:.2f}, over the ${ms.budget:.2f} budget")
    return ms


def _percentile(ascending: list[float], q: float) -> float:
    """Nearest-rank percentile — no interpolation, every answer is a cost that
    actually happened."""
    rank = max(1, math.ceil(q * len(ascending)))
    return ascending[rank - 1]
