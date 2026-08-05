"""Org policy: fetched from Cloud at run start, merged with local config.

An admin who tightens a cap centrally should not have to walk every developer's
laptop. That is the whole feature. Everything else here exists to keep it from
costing anybody anything.

Four properties, in the order they were argued for:

1. **Gated by `cloud:`.** An install that has not opted into Cloud runs none of
   this — no credentials read, no socket opened, no timeout waited out. The gate
   is `cloud.enabled`, the same one cloud.py uses, so an install is connected or
   not connected and never half of each.

2. **Fails open.** Every remote failure — refused connection, timeout, 4xx, 5xx,
   truncated JSON, JSON of the wrong shape, JSON with no policy in it — leaves
   the run on local config and records `source: local`. The halt is the free
   product's one promise; it does not get to depend on a reachable server. Note
   which way this cuts: failing open means a cloud policy CANNOT be relied on to
   tighten a machine that is offline. Cloud's own server-side records are where
   an org proves what it enforced. This is distribution, not enforcement.

3. **Never loosens.** The merge takes the lower cap and the union of
   approval-gated phases. A remote policy more permissive than local config
   changes nothing, so connecting to Cloud can only ever make a run stricter.

4. **Fetched once per run**, from session.ensure, before the first phase opens.

The applied policy and its source go into the trace as a `policy_applied` event,
which is what lets a signed export state which cap was in force rather than
which cap someone says was configured.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .cloud import USER_AGENT, load_token
from .data_types import EventRecord

# Two seconds. Long enough for a healthy round trip, short enough that a black
# hole between here and Cloud costs a run two seconds and nothing else.
TIMEOUT = 2.0


@dataclass(frozen=True)
class Policy:
    max_run_cost: float | None = None
    month_budget: float | None = None
    require_approval_phases: tuple[str, ...] = field(default_factory=tuple)
    # local | cloud | merged — which side each applied value came from. `local`
    # covers both "no policy arrived" and "one arrived and was looser in every
    # field", because in both cases local config is what is in force.
    source: str = "local"


def _cap(value) -> float | None:
    """A cap is a number or it is absent. A string, a bool or a negative number
    is a broken policy, and a broken policy must not become a cap."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _phases(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({p.strip() for p in value if isinstance(p, str) and p.strip()}))


def local_policy(cfg) -> Policy:
    d = getattr(cfg, "defaults", None)
    return Policy(max_run_cost=_cap(getattr(d, "max_run_cost", None)),
                  month_budget=_cap(getattr(d, "month_budget", None)),
                  require_approval_phases=_phases(getattr(d, "require_approval_phases", None)))


def parse(body) -> Policy | None:
    """Turn a decoded response into a policy, or None if there is nothing usable
    in it. A workspace that has set no caps and a workspace that answered with
    something unreadable are the same answer here, because local config is what
    is in force either way. `updated_at` and `updated_by` are for the dashboard.
    """
    if not isinstance(body, dict):
        return None
    parsed = Policy(max_run_cost=_cap(body.get("max_run_cost")),
                    month_budget=_cap(body.get("monthly_budget")),
                    require_approval_phases=_phases(body.get("require_approval_phases")))
    if (parsed.max_run_cost is None and parsed.month_budget is None
            and not parsed.require_approval_phases):
        return None                  # every key present was unusable
    return parsed


def fetch(cloud_cfg, token: str, timeout: float = TIMEOUT) -> Policy | None:
    """GET the workspace policy. Returns None on any failure whatsoever.

    The bare `except Exception` is the point of the function, not an oversight:
    the caller is a run that must proceed, and enumerating every way a socket
    can disappoint is how one of them gets missed.
    """
    url = str(getattr(cloud_cfg, "url", "") or "").rstrip("/")
    if not url:
        return None
    req = urllib.request.Request(
        f"{url}/v1/policy",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse(json.loads(resp.read().decode("utf-8")))
    except Exception:
        return None


def merge(local: Policy, remote: Policy | None) -> Policy:
    """Most restrictive wins: the lower cap, the union of gated phases."""
    if remote is None:
        return local
    from_cloud = from_local = False
    caps: dict[str, float | None] = {}
    for name in ("max_run_cost", "month_budget"):
        mine, theirs = getattr(local, name), getattr(remote, name)
        if theirs is not None and (mine is None or theirs < mine):
            caps[name], from_cloud = theirs, True
        else:
            caps[name] = mine
            if mine is not None:
                from_local = True
    phases = tuple(sorted(set(local.require_approval_phases) | set(remote.require_approval_phases)))
    if set(phases) - set(local.require_approval_phases):
        from_cloud = True
    if local.require_approval_phases:
        from_local = True
    if not from_cloud:
        source = "local"             # a policy arrived and tightened nothing
    elif from_local:
        source = "merged"
    else:
        source = "cloud"
    return Policy(max_run_cost=caps["max_run_cost"], month_budget=caps["month_budget"],
                  require_approval_phases=phases, source=source)


def apply(run, token: str | None = None, timeout: float = TIMEOUT) -> Policy:
    """Resolve the policy for one run, put it in force, and record it.

    Called once, from session.ensure, before the first phase opens — the caps
    have to be settled before anything can spend against them.
    """
    local = local_policy(run.cfg)
    remote = None
    cloud_cfg = getattr(run.cfg, "cloud", None)
    if cloud_cfg is not None and getattr(cloud_cfg, "enabled", False):
        try:
            token = token or load_token()
            if token:
                remote = fetch(cloud_cfg, token, timeout)
        except Exception:
            remote = None
    applied = merge(local, remote)
    run.policy = applied
    defaults = run.cfg.defaults
    # Write back only a cap that exists. `applied` is None wherever neither side
    # supplied a readable number, and a configured-but-unreadable local value
    # (a negative cap, say — the config model permits one) is still a cap the
    # run was under. Erasing it here would be this module loosening a run.
    if applied.max_run_cost is not None:
        defaults.max_run_cost = applied.max_run_cost
    if applied.month_budget is not None:
        defaults.month_budget = applied.month_budget
    defaults.require_approval_phases = list(applied.require_approval_phases)
    try:
        run.tracer.event(EventRecord(
            adw_id=run.adw_id, phase_id="", type="log", name="policy_applied",
            payload={"max_run_cost": applied.max_run_cost,
                     "month_budget": applied.month_budget,
                     "require_approval_phases": list(applied.require_approval_phases),
                     "source": applied.source}))
        if applied.source != "local":
            cap = ("" if applied.max_run_cost is None
                   else f" - max_run_cost ${applied.max_run_cost:.2f}")
            run.console.note(f"org policy applied ({applied.source}){cap}")
    except Exception:
        pass                         # recording the policy must not break the run
    return applied
