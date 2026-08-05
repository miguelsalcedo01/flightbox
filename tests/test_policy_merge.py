# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Org policy distribution, from the free side of the line.

This tests the riskiest code in the product: it runs on every run, including
runs by people who have never paid anything. Four properties matter more than
the feature itself, and each has its own block below.

  1. GATED. An install with no `cloud:` block executes none of it — no token
     read, no socket, no timeout. The gate is `cloud.enabled`, the same one
     cloud.py uses, so there is one answer to "is this install connected".

  2. FAILS OPEN. Unreachable host, timeout, 4xx, 5xx, non-JSON, JSON of the
     wrong shape, JSON with no policy keys — every one of them leaves the run
     on local config. The halt must not depend on the network, so every mode
     is listed here rather than one standing in for the rest.

  3. NEVER LOOSENS. A remote cap above the local one changes nothing. Tested
     from both sides, because a merge that takes the wrong end of the
     comparison passes any test that only ever tightens.

  4. FETCHED ONCE. Per run, not per phase.

Run:  uv run tests/test_policy_merge.py
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

TPL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "flightbox" / "templates"
sys.path.insert(0, str(TPL / "adws"))

from adw_modules import cloud, policy  # noqa: E402
from adw_modules.data_types import FLIGHTBOXConfig  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    # ASCII only: a Windows cp1252 console has choked on a test's own output
    # here before, and a test that dies printing its name is worse than none.
    print(f"[{'PASS' if got == want else 'FAIL'}] {label}")


def cfg_of(text: str) -> FLIGHTBOXConfig:
    return FLIGHTBOXConfig(**(yaml.safe_load(text) or {}))


class FakeTracer:
    def __init__(self):
        self.events = []

    def event(self, record):
        self.events.append(record)
        return "evt_fake"


class FakeConsole:
    def note(self, *_a, **_k):
        pass


class FakeRun:
    """Enough of runner.Run for policy.apply: config, tracer, console, phases."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.adw_id = "test1234"
        self.tracer = FakeTracer()
        self.console = FakeConsole()
        self.phases = []


# ── a real server, so the failure modes are real ────────────────────────────
class Handler(BaseHTTPRequestHandler):
    body = b'{"max_run_cost": 5, "monthly_budget": 50, "require_approval_phases": ["deploy"]}'
    status = 200
    delay = 0.0
    hits = 0
    auth_seen: list[str] = []

    def do_GET(self):
        Handler.hits += 1
        Handler.auth_seen.append(self.headers.get("Authorization", ""))
        if Handler.delay:
            time.sleep(Handler.delay)
        self.send_response(Handler.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(Handler.body)))
        self.end_headers()
        self.wfile.write(Handler.body)

    def log_message(self, *_a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}"


def remote(cfg_text: str, *, body=None, status=200, delay=0.0, timeout=1.0):
    """Run policy.apply against the live stub with the given response."""
    Handler.body = body if body is not None else Handler.body
    Handler.status = status
    Handler.delay = delay
    run = FakeRun(cfg_of(cfg_text))
    applied = policy.apply(run, token="fbx_live_test", timeout=timeout)
    return run, applied


LIVE = f"cloud:\n  sync: metadata\n  url: {URL}\n"


# ── 1. GATED ────────────────────────────────────────────────────────────────
# Not "returns local" — never asks. `source: local` is the answer for a dozen
# other reasons too, so asserting it proves nothing about the gate. What proves
# the gate is that the two things which touch the outside world are never
# called: fail-open would swallow an exception raised from either and the run
# would look identical from the outside.
tripped: list[str] = []

real_fetch, real_load_token = policy.fetch, policy.load_token
policy.fetch = lambda *a, **k: tripped.append("fetch")
policy.load_token = lambda *a, **k: tripped.append("load_token")
try:
    for label, text in [("no cloud block", "defaults:\n  max_run_cost: 10"),
                        ("sync: off", "defaults:\n  max_run_cost: 10\ncloud:\n  sync: off"),
                        ("sync: no", "cloud:\n  sync: no")]:
        tripped.clear()
        run = FakeRun(cfg_of(text))
        applied = policy.apply(run)
        check(f"gate - {label}: nothing reached out", (tripped, applied.source), ([], "local"))
finally:
    policy.fetch, policy.load_token = real_fetch, real_load_token

check("gate - unconnected install still keeps its own cap",
      policy.apply(FakeRun(cfg_of("defaults:\n  max_run_cost: 10"))).max_run_cost, 10.0)


# ── 2. FAILS OPEN, every mode ───────────────────────────────────────────────
LOCAL10 = "defaults:\n  max_run_cost: 10\n  month_budget: 100\n"
DEAD = LOCAL10 + "cloud:\n  sync: metadata\n  url: http://127.0.0.1:9\n"

_, applied = FakeRun(cfg_of(DEAD)), policy.apply(FakeRun(cfg_of(DEAD)), token="t", timeout=1.0)
check("fail open - connection refused -> local", (applied.source, applied.max_run_cost),
      ("local", 10.0))

_, applied = remote(LOCAL10 + LIVE, delay=1.0, timeout=0.3)
check("fail open - timeout -> local", (applied.source, applied.max_run_cost), ("local", 10.0))
check("fail open - the shipped timeout is 2 seconds", policy.TIMEOUT, 2.0)

for status in (400, 401, 403, 404, 500, 503):
    _, applied = remote(LOCAL10 + LIVE, body=b'{"max_run_cost": 1}', status=status)
    check(f"fail open - HTTP {status} -> local", (applied.source, applied.max_run_cost),
          ("local", 10.0))

for label, body in [("not json", b"<html>nope</html>"),
                    ("truncated json", b'{"max_run_cost": 1'),
                    ("json array", b'[1, 2, 3]'),
                    ("json string", b'"tightened"'),
                    ("json null", b"null"),
                    ("empty body", b""),
                    ("no policy keys", b'{"updated_at": "now", "updated_by": "sam"}'),
                    ("keys of the wrong type", b'{"max_run_cost": "cheap", '
                                               b'"require_approval_phases": "deploy"}')]:
    _, applied = remote(LOCAL10 + LIVE, body=body)
    check(f"fail open - {label} -> local", (applied.source, applied.max_run_cost),
          ("local", 10.0))

# Fail-open is deliberately two layers deep — fetch swallows, and apply swallows
# again — so apply alone cannot show that fetch holds its own end. Drive fetch
# directly for the modes that are not socket errors.
class Stub:
    def __init__(self, url):
        self.url = url


for label, body, status in [("HTTP 500", b'{"max_run_cost": 1}', 500),
                            ("not json", b"<html>", 200),
                            ("truncated json", b'{"max_run_cost": 1', 200),
                            ("json array", b"[1,2,3]", 200),
                            ("empty body", b"", 200)]:
    Handler.body, Handler.status, Handler.delay = body, status, 0.0
    check(f"fetch itself returns None on {label}",
          policy.fetch(Stub(URL), "t", timeout=1.0), None)

Handler.status = 200
check("fetch returns None for a url-less config", policy.fetch(Stub(""), "t"), None)

# A fetch that raises something nobody anticipated must still not reach the run.
real_fetch = policy.fetch
policy.fetch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    applied = policy.apply(FakeRun(cfg_of(LOCAL10 + LIVE)), token="t")
    check("fail open - unexpected exception -> local", (applied.source, applied.max_run_cost),
          ("local", 10.0))
finally:
    policy.fetch = real_fetch


# ── 3. NEVER LOOSENS ────────────────────────────────────────────────────────
_, applied = remote(LOCAL10 + LIVE, body=b'{"max_run_cost": 40, "monthly_budget": 2000}')
check("never loosens - remote cap ABOVE local is ignored",
      (applied.max_run_cost, applied.month_budget, applied.source), (10.0, 100.0, "local"))

_, applied = remote(LOCAL10 + LIVE, body=b'{"max_run_cost": 4, "monthly_budget": 40}')
check("tightens - remote cap BELOW local wins",
      (applied.max_run_cost, applied.month_budget, applied.source), (4.0, 40.0, "cloud"))

_, applied = remote(LOCAL10 + LIVE, body=b'{"max_run_cost": 4, "monthly_budget": 2000}')
check("mixed - lower of each end, source merged",
      (applied.max_run_cost, applied.month_budget, applied.source), (4.0, 100.0, "merged"))

_, applied = remote("defaults: {}\n" + LIVE, body=b'{"max_run_cost": 7}')
check("uncapped local adopts the remote cap",
      (applied.max_run_cost, applied.source), (7.0, "cloud"))

_, applied = remote(LOCAL10 + LIVE, body=b'{"monthly_budget": 2000}')
check("remote sends only a looser budget - nothing changes",
      (applied.max_run_cost, applied.month_budget, applied.source), (10.0, 100.0, "local"))

# Approval phases are a UNION, which is the restrictive direction for a set:
# neither side can drop a phase the other requires a human for.
GATED = LOCAL10 + "  require_approval_phases: [deploy]\n"

# Disjoint on purpose. Overlapping sets make a union indistinguishable from
# "take whatever the remote sent", which is exactly the bug worth catching.
_, applied = remote(GATED + LIVE, body=b'{"require_approval_phases": ["migrate"]}')
check("approval phases union", applied.require_approval_phases, ("deploy", "migrate"))

# An empty list alongside a real cap: a workspace that has set a policy and put
# no phases in it must not be able to unlock a phase the laptop's own config
# insists on. (Sent with a cap because a body of nothing but an empty list is
# no policy at all, and is covered above.)
_, applied = remote(GATED + LIVE,
                    body=b'{"max_run_cost": 4, "require_approval_phases": []}')
check("remote cannot drop a locally required approval",
      (applied.require_approval_phases, applied.source), (("deploy",), "merged"))

_, applied = remote("defaults: {}\n" + LIVE, body=b'{"require_approval_phases": ["deploy"]}')
check("remote-only approval phase is adopted",
      (applied.require_approval_phases, applied.source), (("deploy",), "cloud"))


# ── the applied policy reaches the governor and the trace ───────────────────
run, applied = remote(LOCAL10 + LIVE, body=b'{"max_run_cost": 4, "monthly_budget": 40, '
                                            b'"require_approval_phases": ["deploy"]}')
check("the merged cap is the cap the run enforces", run.cfg.defaults.max_run_cost, 4.0)
check("the merged budget is the budget the run reports", run.cfg.defaults.month_budget, 40.0)
check("the merged approval phases reach config",
      run.cfg.defaults.require_approval_phases, ["deploy"])

recorded = [e for e in run.tracer.events if e.name == "policy_applied"]
check("exactly one policy_applied event", len(recorded), 1)
check("the trace records which cap was in force",
      (recorded[0].payload["max_run_cost"], recorded[0].payload["source"]), (4.0, "cloud"))
check("the trace records the approval phases",
      recorded[0].payload["require_approval_phases"], ["deploy"])

# The write-back is the one place a policy fetch can hand the run a LOOSER
# config than it was given. Nothing in the config model rejects a negative cap
# and _cap() maps one to None, so writing that None back would uncap a run that
# was halting on its first send.
loose = FakeRun(cfg_of("defaults:\n  max_run_cost: -1\n  month_budget: -5\n"))
policy.apply(loose)
check("a local cap _cap refuses is left alone, not erased",
      (loose.cfg.defaults.max_run_cost, loose.cfg.defaults.month_budget), (-1.0, -5.0))

# An export has to be able to read this back, and metadata mode strips payloads
# by default. If policy_applied is not on the governance allowlist the numbers
# that prove the cap vanish on the way to Cloud.
kept = cloud._governance_payload({"name": "policy_applied",
                                  "payload_json": json.dumps(recorded[0].payload)})
check("policy_applied survives metadata sync", json.loads(kept or "{}").get("source"), "cloud")
check("and keeps the cap that was in force",
      json.loads(kept or "{}").get("max_run_cost"), 4.0)

# A run with no cloud block still records what held. The export claim is about
# every run, not only connected ones.
run = FakeRun(cfg_of(LOCAL10))
policy.apply(run)
check("an unconnected run still records its policy",
      [(e.payload["source"], e.payload["max_run_cost"])
       for e in run.tracer.events if e.name == "policy_applied"], [("local", 10.0)])


# ── 4. FETCHED ONCE PER RUN ─────────────────────────────────────────────────
Handler.hits = 0
Handler.auth_seen = []
run, _ = remote(LOCAL10 + LIVE, body=b'{"max_run_cost": 4}')
check("one apply, one request", Handler.hits, 1)
check("the token is presented as a bearer", Handler.auth_seen[-1], "Bearer fbx_live_test")

# session.ensure is the only caller, and it runs once per run — a phase must
# not be able to reach it.
runner_src = (TPL / "adws" / "adw_modules" / "runner.py").read_text("utf-8")
session_src = (TPL / "adws" / "adw_modules" / "session.py").read_text("utf-8")
check("runner.py never fetches policy", "policy.apply" in runner_src, False)
check("session.ensure applies policy exactly once", session_src.count("policy.apply"), 1)

# No token, no request: the workspace is unidentifiable without one.
Handler.hits = 0
run = FakeRun(cfg_of(LOCAL10 + LIVE))
applied = policy.apply(run, token=None)
check("no token -> no request, source local", (Handler.hits, applied.source), (0, "local"))


print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
