"""Approval phases: the blocking human gate inside a run.

The pattern everywhere else in the factory is "agent proposes, code disposes".
An approval is the third case: CODE proposes, a HUMAN disposes. The run writes
a pending row into the `approvals` table and stops moving until a person
settles it — nothing else in the chain runs while it waits.

Two ways a decision arrives, one data path:
  * interactive — stdin is a TTY, so the run prompts y/n right there and
    writes the decision through the tracer itself;
  * headless — the run polls its row while an engineer settles it from any
    other terminal with `just approve <adw_id> <name>` (or `just deny`).
    `just pending` lists what is waiting.

Denial and timeout are the same outcome by design: ApprovalDenied unwinds
through run.phase(), the phase fails, the session finalizes fail. A
governance gate that defaults open on silence is not a gate.
"""

from __future__ import annotations

import json
import sys
import time

from .data_types import ApprovalParams, EventRecord, Phase


class ApprovalDenied(RuntimeError):
    """The human said no — or nobody said yes inside the timeout."""


def decide(run, phase: Phase, params: ApprovalParams) -> str:
    """Block until a human settles this approval. Returns who approved.

    Denial or timeout raises ApprovalDenied, which fails the phase and the
    run. Only valid inside an engineer-kind phase: the lane in the trace
    should show a human when a human is the one the run is waiting on.
    """
    if phase.params.kind != "engineer":
        raise RuntimeError("ph.approval() is only valid inside an engineer phase — "
                           "the trace lane must show who the run is waiting on")

    approval_id = run.tracer.approval_request(
        phase, params.name, params.description, json.dumps(params.details))
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, phase_id=phase.phase_id,
        type="approval_requested", name=params.name,
        payload={"approval_id": approval_id, "description": params.description,
                 "details": params.details,
                 "timeout_seconds": params.timeout_seconds}))
    run.console.note(f"APPROVAL REQUIRED — {params.name}: {params.description}")
    for key, value in params.details.items():
        run.console.note(f"  {key}: {value}")

    if sys.stdin.isatty():
        answer = input(f"approve {params.name!r}? [y/N] ").strip().lower()
        run.tracer.approval_decide(approval_id, answer in ("y", "yes"),
                                   run.engineer or "engineer")
    else:
        run.console.note(f"waiting (timeout {params.timeout_seconds:.0f}s) — settle with: "
                         f"just approve {run.adw_id} {params.name}   "
                         f"or: just deny {run.adw_id} {params.name}")

    status, decided_by = _wait(run, approval_id, params)
    if status == "approved":
        run.tracer.event(EventRecord(
            adw_id=run.adw_id, phase_id=phase.phase_id,
            type="approval_granted", name=params.name,
            payload={"approval_id": approval_id, "decided_by": decided_by}))
        run.console.note(f"approved by {decided_by}")
        return decided_by

    if status == "pending":                       # nobody answered in time
        run.tracer.approval_decide(approval_id, False, "timeout")
        status, decided_by = "timeout", "timeout"
    run.tracer.event(EventRecord(
        adw_id=run.adw_id, phase_id=phase.phase_id,
        type="approval_denied", name=params.name,
        payload={"approval_id": approval_id, "status": status,
                 "decided_by": decided_by}))
    raise ApprovalDenied(
        f"approval {params.name!r} was {'never granted (timeout)' if status == 'timeout' else f'denied by {decided_by}'}")


def _wait(run, approval_id: str, params: ApprovalParams) -> tuple[str, str]:
    """Poll the row until it settles or the timeout runs out."""
    deadline = time.monotonic() + params.timeout_seconds
    while True:
        status, decided_by = run.tracer.approval_status(approval_id)
        if status != "pending" or time.monotonic() >= deadline:
            return status, decided_by
        time.sleep(min(params.poll_seconds, max(0.0, deadline - time.monotonic())))
