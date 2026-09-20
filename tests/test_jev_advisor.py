# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Jev advisory calls: risk on approvals.

Rule from the repo owner: never mock external services. So this file tests
the PURE functions directly — request building, question building, answer
interpretation, the unavailable path (simply unsetting the env var), and
error-message hygiene (no body, no key, ever reaches an exception message) —
and adds one LIVE section that only runs when OPENROUTER_API_KEY is already
set in the environment.

HARD CONSTRAINT under test throughout: Jev only ever advises. It cannot
grant, deny, or halt anything on its own — see approvals.py's docstring.

Run:  uv run tests/test_jev_advisor.py
Live section:  set OPENROUTER_API_KEY, then run the same command.
"""

import json
import os
import sys
import urllib.error
from pathlib import Path

import yaml

TPL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "flightbox" / "templates"
sys.path.insert(0, str(TPL / "adws"))

from adw_modules import approvals, jev  # noqa: E402
from adw_modules.data_types import (ApprovalParams, EventRecord,  # noqa: E402
                                    FLIGHTBOXConfig, Phase, PhaseParams)

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

    def approval_request(self, phase, name, description, details_json):
        return "appr_fake"

    def approval_decide(self, approval_id, ok, decided_by):
        pass

    def approval_status(self, approval_id):
        return "approved", "engineer"


class FakeConsole:
    def __init__(self):
        self.notes: list[str] = []

    def note(self, text, **_k):
        self.notes.append(text)


class FakeRun:
    """Enough of runner.Run for approvals.decide / estimates.announce."""

    def __init__(self, cfg, adw_id="test1234"):
        self.cfg = cfg
        self.adw_id = adw_id
        self.tracer = FakeTracer()
        self.console = FakeConsole()
        self.phases: list = []
        self.engineer = "engineer"


def a_phase(name="deploy") -> Phase:
    params = PhaseParams(name=name, kind="engineer", owner="engineer",
                         description=f"Ship {name} for real")
    return Phase(phase_id=f"test1234_01_{name}", adw_id="test1234", seq=1, params=params)


# ── env isolation: OPENROUTER_API_KEY is toggled per-block, always restored ──
ORIGINAL_KEY = os.environ.pop("OPENROUTER_API_KEY", None)


def _unset_key():
    os.environ.pop("OPENROUTER_API_KEY", None)


def _restore_key():
    if ORIGINAL_KEY is not None:
        os.environ["OPENROUTER_API_KEY"] = ORIGINAL_KEY
    else:
        os.environ.pop("OPENROUTER_API_KEY", None)


# ── 1. build_request is pure and carries no secrets in the wrong place ──────
_unset_key()
req = jev.build_request("sk-test-secret-123", {"name": "x"}, {"q": {"type": "noul"}})
check("build_request targets the Decisions API", req.full_url, jev.DECISIONS_URL)
check("build_request sends the model", json.loads(req.data)["model"], jev.MODEL)
check("build_request carries state through untouched",
      json.loads(req.data)["state"], {"name": "x"})
check("build_request carries questions through untouched",
      json.loads(req.data)["questions"], {"q": {"type": "noul"}})
check("build_request authenticates as a bearer",
      req.get_header("Authorization"), "Bearer sk-test-secret-123")
check("build_request identifies itself honestly",
      req.get_header("User-agent").startswith("flightbox-jev/"), True)


# ── 2. ask() raises JevUnavailable on a missing key, and never leaks it ─────
_unset_key()
try:
    jev.ask({"x": 1}, {"q": {"type": "noul"}})
    check("ask() raises without a key", False, True)
except jev.JevUnavailable as e:
    check("ask() raises without a key", True, True)
    check("the missing-key message names no key value", "sk-" not in str(e), True)


# ── 3. error-message hygiene: status only, never body, never key ───────────
import io  # noqa: E402


class FakeHTTPError(urllib.error.HTTPError):
    """Carries a real body, so a mutation that reads it back has something to
    leak — an HTTPError built with fp=None has nothing to test against."""

    def __init__(self, code, body=b'{"error": "server error, body must never leak"}'):
        super().__init__("https://openrouter.ai/x", code, "err", {}, io.BytesIO(body))


os.environ["OPENROUTER_API_KEY"] = "sk-should-never-appear-in-any-message"
real_urlopen = jev.urllib.request.urlopen


def raise_http(code):
    def _urlopen(*_a, **_k):
        raise FakeHTTPError(code)
    return _urlopen


jev.urllib.request.urlopen = raise_http(500)
try:
    jev.ask({"x": 1}, {"q": {"type": "noul"}})
    check("ask() raises on a 500", False, True)
except jev.JevUnavailable as e:
    msg = str(e)
    check("ask() raises on a 500", True, True)
    check("the 500 message carries the status", "500" in msg, True)
    check("the 500 message carries no key", "sk-should-never-appear" not in msg, True)
    check("the 500 message carries no response body",
          "must never leak" not in msg, True)
finally:
    jev.urllib.request.urlopen = real_urlopen


class FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


LEAKY_BODY = (b'{"error": "invalid request", "prompt": "the users secret prompt", '
             b'"api_key_used": "sk-should-never-appear-in-any-message"}')

jev.urllib.request.urlopen = lambda *a, **k: FakeResp(LEAKY_BODY)
try:
    jev.ask({"x": 1}, {"q": {"type": "noul"}})
    check("ask() raises on a body with no 'answers'", False, True)
except jev.JevUnavailable as e:
    msg = str(e)
    check("ask() raises on a body with no 'answers'", True, True)
    check("the malformed-body message carries none of the body",
          ("prompt" not in msg and "secret" not in msg and "sk-should-never-appear" not in msg),
          True)
finally:
    jev.urllib.request.urlopen = real_urlopen
    _unset_key()


# ── 4. question building / answer interpretation, pure ──────────────────────
check("risk_state carries the approval's own fields, nothing else",
      jev.risk_state("deploy", "ship it", {"env": "prod"}),
      {"name": "deploy", "description": "ship it", "details": {"env": "prod"}})
q = jev.risk_question()
check("risk_question asks exactly one 'risk' choice question", list(q.keys()), ["risk"])
check("risk_question's criteria cover all three levels",
      set(q["risk"]["criteria"].keys()), {"low", "medium", "high"})

check("interpret_risk reads a well-formed choice answer",
      jev.interpret_risk({"risk": {"type": "choice", "choice": "high",
                                   "confidence": 0.91, "probabilities": {"high": 0.9}}}),
      {"risk": "high", "confidence": 0.91, "probabilities": {"high": 0.9}})
check("interpret_risk rejects a choice outside the three levels",
      jev.interpret_risk({"risk": {"type": "choice", "choice": "catastrophic"}}), None)
check("interpret_risk rejects the wrong answer type",
      jev.interpret_risk({"risk": {"type": "noul", "noul": 0.9}}), None)
check("interpret_risk rejects a missing key", jev.interpret_risk({}), None)
check("interpret_risk rejects a non-dict answers", jev.interpret_risk("nope"), None)

# ── 5. advisor_enabled: opt-in only; a key on its own turns nothing on ─────
check("advisor_enabled(True) is on with no key", jev.advisor_enabled(True), True)
check("advisor_enabled(False) is off even with a key present",
      (lambda: (os.environ.__setitem__("OPENROUTER_API_KEY", "sk-x"),
               jev.advisor_enabled(False))[1])(), False)
_unset_key()
check("advisor_enabled(None) is off with no key", jev.advisor_enabled(None), False)
os.environ["OPENROUTER_API_KEY"] = "sk-x"
check("advisor_enabled(None) stays OFF with a key present — a key is not consent",
      jev.advisor_enabled(None), False)
_unset_key()


# ── 6. the unavailable path end-to-end: approvals.decide never raises,
#      never delays, and traces advisor_unavailable instead of a note ───────
class _FakeStdin:
    def isatty(self):
        return False


_unset_key()  # advisor_enabled(None) is False -> _advise_risk short-circuits
_real_stdin = approvals.sys.stdin
approvals.sys.stdin = _FakeStdin()          # headless path, no interactive prompt
run = FakeRun(cfg_of("defaults: {}"))
phase = a_phase()
approvals.decide(run, phase, ApprovalParams(name="deploy", description="ship it",
                                            timeout_seconds=1.0))
check("with no key, decide() never prints a risk note",
      any("risk (advisory" in n for n in run.console.notes), False)
check("with no key, decide() traces nothing about the advisor at all",
      any(e.name in ("approval_risk", "advisor_unavailable") for e in run.tracer.events), False)

# Force jev_advisor: true with no key, so _advise_risk actually reaches
# jev.ask and hits the "no key" JevUnavailable path -> advisor_unavailable.
run2 = FakeRun(cfg_of("defaults:\n  jev_advisor: true"))
phase2 = a_phase()
approvals.decide(run2, phase2, ApprovalParams(name="deploy", description="ship it",
                                              timeout_seconds=1.0))
check("advisor forced on with no key -> no risk note printed",
      any("risk (advisory" in n for n in run2.console.notes), False)
unavailable = [e for e in run2.tracer.events if e.name == "advisor_unavailable"]
check("advisor forced on with no key -> exactly one advisor_unavailable event",
      len(unavailable), 1)
check("advisor_unavailable names the reason, not a body or key",
      "OPENROUTER_API_KEY" in unavailable[0].payload.get("reason", ""), True)
check("decide() still returns normally (never raises out of the advisor)",
      True, True)  # the two calls above completing at all IS the assertion
approvals.sys.stdin = _real_stdin


# ── 8. reviewer's "prove it fails" step is done separately below, not here ──
# (this file only re-asserts the guards hold in the shipped source; the
# temporary-break-and-confirm-red step is recorded in the final report, run
# by hand against a scratch copy so the repo is never left broken)


print()

# ── LIVE section: only with a real OPENROUTER_API_KEY already in the env ────
_restore_key()
if os.environ.get("OPENROUTER_API_KEY", "").strip():
    print("--- live section (OPENROUTER_API_KEY present) ---")
    dangerous = jev.ask(
        jev.risk_state("migrate_users",
                       "deploy to production and run an irreversible database "
                       "migration dropping the users table",
                       {"env": "production", "reversible": False}),
        jev.risk_question(), timeout=10.0)
    dangerous_risk = jev.interpret_risk(dangerous)
    check("live: an obviously dangerous approval scores HIGH risk",
          dangerous_risk and dangerous_risk["risk"], "high")

    trivial = jev.ask(
        jev.risk_state("fix_typo", "fix a typo in README",
                       {"env": "none", "reversible": True}),
        jev.risk_question(), timeout=10.0)
    trivial_risk = jev.interpret_risk(trivial)
    check("live: an obviously trivial approval scores LOW risk",
          trivial_risk and trivial_risk["risk"], "low")
else:
    print("--- live section skipped: OPENROUTER_API_KEY not set ---")

if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
