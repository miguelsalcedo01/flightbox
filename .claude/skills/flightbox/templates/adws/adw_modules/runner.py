"""The Run object: config + adw_id + agent_map + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import agents, approvals, git_helper
from .console import Console
from .data_types import (AgentCall, ApprovalParams, EnvelopeBase, EventRecord,
                         Phase, PhaseParams)
from .utils import ensure_dir, now_iso


class BudgetExceeded(RuntimeError):
    """A spend cap was crossed. Raised from add_usage, AFTER the crossing send
    is billed and traced — enforcement is at call boundaries, because a coding
    agent bills per send and a subprocess mid-send cannot be un-spent. The
    raise unwinds through run.phase(), which fails the phase and finalizes the
    session, so the next send never starts. That is the governance guarantee:
    the cap stops the run, it does not merely report the overrun."""


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(EventRecord(adw_id=self.run.adw_id,
                                          phase_id=self.phase.phase_id,
                                          type="log", name=self.phase.params.name,
                                          payload=payload))
        self.run.console.note(", ".join(f"{k}: {v}" for k, v in payload.items()))
        if self.phase.params.kind == "engineer" and "input" in payload:
            self.run.tracer.session_request(self.run.adw_id, str(payload["input"]))

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        return agents.execute(self.run, self.phase, call)

    def approval(self, params: ApprovalParams) -> str:
        """Block until a human settles this. Returns who approved; denial or
        timeout raises ApprovalDenied and fails the phase. Engineer-kind
        phases only — the trace lane shows who the run is waiting on."""
        return approvals.decide(self.run, self.phase, params)


class Run:
    def __init__(self, cfg, adw_id: str, tracer, engineer: str):
        self.cfg = cfg
        self.adw_id = adw_id
        self.tracer = tracer
        self.console = Console(tracer, adw_id)
        self.engineer = engineer
        self.phases: list[Phase] = []
        self.tokens = 0
        self.cost = 0.0
        # Per-agent dollars this run, for the per-agent max_cost cap. Keyed by
        # agent name; a joined run (--adw-id) starts these at zero, so caps are
        # per-invocation, not per-lifetime-of-session.
        self.agent_cost: dict[str, float] = {}
        # Approach warnings already given ("run:75", "agent:planner:90") — a
        # threshold speaks once. You cannot stop a send mid-flight, so the
        # useful thing is seeing the line coming, not hearing about it again
        # on every call after.
        self._budget_warned: set[str] = set()
        self._seq = tracer.max_phase_seq(adw_id)   # a joined run continues the sequence
        self.repo_root = git_helper.repo_root()    # where every agent is spawned to work
        self.session_dir = ensure_dir(Path(cfg.defaults.data_dir) / "sessions" / adw_id)
        self.context_handoff_dir = ensure_dir(self.session_dir / "context_handoff")
        self._agent_map_path = self.session_dir / "agent_map.json"
        self.agent_map: dict = (json.loads(self._agent_map_path.read_text())
                                if self._agent_map_path.exists() else {})

    # ── agent map (adw_id -> per-agent coding-agent session ids) ────────────
    def save_agent_map(self, agent: str, entry: dict) -> None:
        self.agent_map[agent] = entry
        self._agent_map_path.write_text(json.dumps(self.agent_map, indent=2))

    # ── usage (run totals mirror what the tracer accumulates in sqlite) ─────
    def add_usage(self, tokens: int, cost: float, agent: str = "") -> None:
        """Bill one send, then enforce the spend caps.

        Ordering matters: the money is already spent, so it is recorded first —
        the trace and the db stay truthful even on the send that crosses a cap.
        Only then does the check raise, halting the run before the next send.
        """
        self.tokens += tokens
        self.cost += cost
        self.tracer.session_add_usage(self.adw_id, tokens, cost)
        if agent:
            self.agent_cost[agent] = self.agent_cost.get(agent, 0.0) + cost
            # Persist it too: the in-memory tally dies with the process, and a trace
            # that cannot say which agent spent the money is only half a record.
            self.tracer.agent_add_usage(self.adw_id, agent, tokens, cost)
        self._enforce_budget(agent)

    def _enforce_budget(self, agent: str) -> None:
        """Raise BudgetExceeded if a configured cap has been crossed."""
        breaches = []
        cap = self.cfg.defaults.max_run_cost
        if cap is not None and self.cost > cap:
            breaches.append(f"run spend ${self.cost:.4f} exceeds max_run_cost ${cap:.4f}")
        if agent:
            spec = next((a for a in self.cfg.agents if a.name == agent), None)
            agent_cap = spec.max_cost if spec else None
            spent = self.agent_cost.get(agent, 0.0)
            if agent_cap is not None and spent > agent_cap:
                breaches.append(f"agent {agent!r} spend ${spent:.4f} "
                                f"exceeds max_cost ${agent_cap:.4f}")
        if not breaches:
            self._warn_if_approaching(agent)
            return
        phase_id = self.phases[-1].phase_id if self.phases else ""
        self.tracer.event(EventRecord(
            adw_id=self.adw_id, phase_id=phase_id,
            type="error", name="budget_exceeded",
            payload={"breaches": breaches, "agent": agent,
                     "run_cost": self.cost,
                     "agent_cost": self.agent_cost,
                     "max_run_cost": self.cfg.defaults.max_run_cost}))
        self.console.note("BUDGET EXCEEDED — halting run: " + "; ".join(breaches))
        self._mention_cloud()
        raise BudgetExceeded("; ".join(breaches))

    def _mention_cloud(self) -> None:
        """One line, once, at the moment the halt proves its worth.

        Shown only when Cloud sync is off — never nag someone who already pays. The halt
        itself is free and always will be; what Cloud adds is being able to show the cap
        held afterwards, from another machine, to someone who wasn't there.
        """
        try:
            cfg = self.cfg
            cloud_cfg = cfg.get("cloud") if isinstance(cfg, dict) else getattr(cfg, "cloud", None)
            mode = (cloud_cfg.get("sync") if isinstance(cloud_cfg, dict)
                    else getattr(cloud_cfg, "sync", None))
            if mode in ("metadata", "full"):
                return                       # already a customer
            self.console.note(
                "This trace is on this machine only. Replay it from anywhere and show "
                "the cap held: https://flightbox.dev"
            )
        except Exception:
            pass                             # a nudge must never affect a run

    # A send cannot be stopped mid-flight — once the call is made, its cost is
    # committed. So the governor's second job is foresight: say "the line is
    # close" while there is still a send boundary left to act on.
    WARN_STEPS = (0.90, 0.75)      # checked highest first; each speaks once

    def _warn_if_approaching(self, agent: str) -> None:
        scopes = [("run", self.cost, self.cfg.defaults.max_run_cost)]
        if agent:
            spec = next((a for a in self.cfg.agents if a.name == agent), None)
            if spec is not None:
                scopes.append((f"agent:{agent}", self.agent_cost.get(agent, 0.0),
                               spec.max_cost))
        for scope, spent, cap in scopes:
            if not cap:
                continue
            for step in self.WARN_STEPS:
                key = f"{scope}:{int(step * 100)}"
                if spent / cap < step or key in self._budget_warned:
                    continue
                # Mark this step AND every lower one: crossing 90% cold must
                # not queue a stale 75% warning behind it.
                self._budget_warned.update(
                    f"{scope}:{int(s * 100)}" for s in self.WARN_STEPS if s <= step)
                phase_id = self.phases[-1].phase_id if self.phases else ""
                self.tracer.event(EventRecord(
                    adw_id=self.adw_id, phase_id=phase_id,
                    type="log", name="budget_warning",
                    payload={"scope": scope, "threshold": step, "spent": spent,
                             "cap": cap, "fraction": spent / cap}))
                self.console.note(
                    f"BUDGET WARNING — {scope} at {spent / cap:.0%} of its"
                    f" ${cap:.2f} cap (${spent:.4f} spent)")
                break

    # ── the phase primitive ─────────────────────────────────────────────────
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase = Phase(phase_id=f"{self.adw_id}_{self._seq:02d}_{params.name}",
                      adw_id=self.adw_id, seq=self._seq, params=params,
                      status="running", started_at=now_iso())
        self.phases.append(phase)
        self.tracer.phase_upsert(phase)
        self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                      type="phase_start", name=params.name,
                                      payload={"kind": params.kind, "owner": params.owner,
                                               "description": params.description}))
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            phase.status = "fail"                      # success must be earned
            phase.error = str(error)[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="error", name=params.name,
                                          payload={"error": phase.error}))
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "fail"}))
            self.tracer.phase_upsert(phase)
            self.tracer.session_finish(self.adw_id, ok=False)
            self.console.phase_ended(phase, time.monotonic() - clock)
            self.console.session_finished(False, self.tokens, self.cost,
                                          self.cfg.observability.db)
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "success"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)

    # ── run outcome ─────────────────────────────────────────────────────────
    def finish(self, accepted: bool = True, reason: str = "") -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the ADW's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not.

        This replaces a `succeeded` property that answered only the first
        question — and, being a property with side effects, wrote the session
        status and printed the banner before the caller's `and test.passed` was
        ever evaluated. A run whose suite never passed was recorded green in the
        db, on the terminal, and in the UI while exiting 1. Anyone reading the
        trace saw success; only a CI job checking `$?` saw the truth. One call
        now settles the db, the banner, and the exit code together, so the three
        cannot disagree.
        """
        phases_ok = bool(self.phases) and all(p.status == "success" for p in self.phases)
        ok = phases_ok and accepted
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(EventRecord(
                adw_id=self.adw_id,
                phase_id=self.phases[-1].phase_id if self.phases else "",
                type="error", name="not_accepted", payload={"reason": note}))
            self.console.note(f"not accepted: {note}")
        self.tracer.session_finish(self.adw_id, ok=ok)
        self.console.session_finished(ok, self.tokens, self.cost, self.cfg.observability.db)
        return 0 if ok else 1
