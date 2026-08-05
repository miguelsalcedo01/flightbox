# Open-core split — architecture plan

Status: **planned, not executed.** The repo went private on 2026-08-04 at zero adoption
(0 stars, 0 forks, 0 clones, 0 views) so nothing has been distributed under MIT to anyone.
That is the window this plan spends.

## Why the repo went private

Flightbox is a derivative of an MIT-licensed orchestrator. MIT permits proprietary
derivatives — it explicitly grants the right to *sublicense* — so building a paid product
on it is legitimate, exactly as Cursor did with VS Code. What was not deliberate was
publishing Flightbox's **own** additions (budget gates, approvals, estimates) as public
MIT. Cursor never open-sourced its additions; that is the whole difference.

MIT's grant only binds toward people who actually obtained a copy. Nobody did. Going
private closed the window cleanly, with no community to disrupt and no rug-pull.

## The seam

Governance touches the recorder at exactly four points. Everything else — tracing,
sessions, phases, agent execution, the local replay UI — is recorder, not governor.

| Point | File | Feature |
|---|---|---|
| `Run.add_usage()` → `_enforce_budget()` / `_warn_if_approaching()` | `adw_modules/runner.py` | budget caps, approach warnings |
| `PhaseHandle.approval()` → `approvals.decide()` | `adw_modules/runner.py` → `approvals.py` | approval gates |
| `session.ensure()` → `estimates.announce()` / `announce_month()` | `adw_modules/session.py` → `estimates.py` | pre-run estimates, month projection |
| `max_run_cost`, `max_cost`, `month_budget`, `ApprovalParams` | `adw_modules/data_types.py` | config surface |

Whole modules that move: `approvals.py`, `estimates.py`.
Methods that move out of `runner.py`: `_enforce_budget`, `_warn_if_approaching`.

## Target shape

| Repo | Visibility | License | Contents |
|---|---|---|---|
| `flightbox` | public | MIT (upstream notice retained) | recorder, trace DB, sessions/phases/agents, local replay UI, hook registry |
| `flightbox-pro` | private | commercial | the governor: budget caps, approach warnings, approvals, estimates |
| `flightbox-cloud` | private | commercial | hosted team layer — aggregation, org policy, alerts |

Enforcement strength differs per tier and that is fine: Cloud is server-side and
genuinely unbypassable; Pro is a licensed local package, deterring companies (its buyers)
rather than determined individuals; Core is free.

## Hook registry (core, MIT)

The core gains one small module. It has no governance logic — it only defines where a
governor may attach.

```python
# adw_modules/hooks.py
_HOOKS: dict[str, list] = {}

def register(event: str, fn) -> None:
    _HOOKS.setdefault(event, []).append(fn)

def emit(event: str, *args, **kwargs) -> None:
    for fn in _HOOKS.get(event, []):
        fn(*args, **kwargs)

def has(event: str) -> bool:
    return bool(_HOOKS.get(event))
```

Core call sites become:

- `Run.add_usage()` — records usage, then `hooks.emit("usage", self, agent)`.
  The recording stays in core and stays truthful; only the *reaction* is pluggable.
- `session.ensure()` — `hooks.emit("session_start", run, adw_name)`.
- `PhaseHandle.approval()` — if `not hooks.has("approval")`, raise a clear
  `FeatureNotAvailable` naming Flightbox Pro. Never fail silently, and never pretend the
  gate held when nothing is enforcing it.

Discovery via `importlib.metadata.entry_points(group="flightbox.plugins")` so an installed
Pro package registers itself with no config.

## Non-negotiables

1. **A missing governor must fail loud, never open.** If `max_run_cost` is set in config
   and no governor is installed, the run must refuse to start — not run uncapped. A budget
   cap that silently does nothing is worse than no cap, because the user believes they are
   protected.
2. **Keep the upstream MIT notice in `LICENSE`.** Required for the derived core, and
   required in any Pro distribution that ships derived code.
3. **The site copy currently says budget gates and approvals are free forever.** It must
   change in the same motion as the split, or it becomes a fresh lie. See `site/index.html`
   pricing section and FAQ.
4. **Do not claw back from anyone who already has it.** Currently nobody does, which is
   the only reason this is clean.

## Where the line sits — decided by research (2026-08-04)

**The halt stays free.** Two independent research passes landed on the same answer, for the
same reason: the closest architectural precedent is **Styra/OPA**, where the enforcement
engine (OPA) is free open source and the paid product (DAS) is the *control plane* —
policy distribution, decision logs, fleet management.

Free core keeps: the recorder, the trace and its **documented schema**, the local replay
UI, and a working single-workspace budget cap with mid-run halt.

Paid is the org layer: multi-policy engine, team/org budget distribution, spend
attribution, exporters, retention, and the signed audit export.

Rationale worth keeping:

- The halt is the adoption wedge and the only thing that gets talked about. It belongs in
  every free install.
- "We stop your agent — for $499" reads as a hostage product. "We stop your agent for
  free, and prove to your client that it held, for $499" reads as a platform.
- It avoids the Docker trap: the free tier does not solve the paid job. The paid job is
  *"prove to my client's compliance team that the policy held, across everyone,
  retroactively"* — genuinely different work.

## Mechanics — decided

| Decision | Choice | Why |
|---|---|---|
| Split mechanism | separate private repo + `importlib.metadata` entry points, group `flightbox.plugins` | `pip install flightbox-pro` activates, `pip uninstall` deactivates. No `if licensed:` branches in the free path. |
| Not GitLab's `ee/` | rejected | a Ruby monkey-patch answer to a 1000-engineer problem; permanent merge tax, and it puts the paid code one copy-paste away |
| Obfuscation (Cython/Nuitka/PyArmor) | **skip entirely** | Cython's own docs say it is not an obfuscation tool; Nuitka leaves strings readable outside its paid tier. Weeks of per-platform build work against an adversary who was never going to pay. |
| Distribution | authenticated private index | the gate belongs at *acquisition*, not runtime |
| Licensing of Pro | short proprietary EULA, source never published | BSL/FSL/SSPL exist to stop hyperscalers reselling published source — not this problem |
| License validation | Ed25519-signed offline license, public key in the wheel, revalidate ~30d with grace | buy it (Keygen), don't build it |
| **Failure mode** | **fail OPEN on network error; closed only on invalid/expired signature** | a governance tool whose halt path depends on a reachable license server is worse than no tool |
| Node-locking | **never** | CI runners are ephemeral; every run looks like a new machine. Seat/org licensing, counted generously. |
| Telemetry | opaque licence id only, never trace contents | the product's core claim is that prompts and costs stay local. Any perceived exfiltration destroys it. |

## Anti-backlash commitments

Every backlash on record (HashiCorp, Redis, Elastic) had the same three triggers:
retroactive relicensing of code people had already built on, ambiguous "competes with us"
restrictions, and no warning. None apply here **because there are no users yet** — that is
the entire advantage of acting now.

Locking these in:

1. The core licence is permissive and **permanent**. Say so in the README on day one and
   never revoke it. The single biggest available mistake is to publish everything
   permissively, grow a community, and relicense in 18 months.
2. The **trace schema and file format stay open**. Never hold a customer's own recorded
   data hostage.
3. State plainly in the README what is free forever and what is paid, before the first
   user arrives.
