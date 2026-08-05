# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""`sync: metadata` must send the governance numbers and nothing else.

The promise printed on the site, in the config, and on the claim page is that
under `metadata` your prompts and your source never leave the machine. Governance
events are the one exception, and they are an exception about SHAPE, not about
trust: they carry what the cap was and what was spent, which are numbers.

The risk being tested is that the exception widens by accident - someone adds a
key to a budget event payload upstream and it starts riding along to a server.
The allowlist is what prevents that, so the test plants hostile keys with obvious
content and asserts none of them survive.

Run:  uv run tests/test_metadata_privacy.py
"""

import json
import sys
from pathlib import Path

TPL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "flightbox" / "templates"
sys.path.insert(0, str(TPL / "adws"))

from adw_modules.cloud import _governance_payload  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")


SECRET = "def launch_codes(): return 'my proprietary source'"

# A budget halt carrying both the numbers we want and content we must never send.
halt = {
    "name": "budget_exceeded",
    "payload_json": json.dumps({
        "breaches": ["run spend $4.83 exceeds max_run_cost $4.50"],
        "run_cost": 4.83,
        "max_run_cost": 4.50,
        "agent_cost": {"builder": 3.1},
        "agent": "builder",
        # everything below is hostile: it must not survive
        "prompt": SECRET,
        "diff": SECRET,
        "file_contents": SECRET,
        "messages": [{"role": "user", "content": SECRET}],
        "some_future_key": SECRET,
    }),
}

out = _governance_payload(halt)
check("governance event keeps a payload", out is not None)
kept = json.loads(out or "{}")

check("cap survives", kept.get("max_run_cost") == 4.50)
check("spend survives", kept.get("run_cost") == 4.83)
check("breach text survives", bool(kept.get("breaches")))
check("per-agent cost survives", kept.get("agent_cost") == {"builder": 3.1})
check("no secret anywhere in the payload", SECRET not in (out or ""),
      "source text was transmitted under metadata mode")
for bad in ("prompt", "diff", "file_contents", "messages", "some_future_key"):
    check(f"'{bad}' stripped", bad not in kept)

# Ordinary events keep nothing at all.
for name in ("tool_call", "agent_end", "handoff", "log", "phase_start"):
    check(f"non-governance event '{name}' fully stripped",
          _governance_payload({"name": name, "payload_json": json.dumps({"prompt": SECRET})}) is None)

# An `error` event that is not a governance event must not sneak through: agent
# errors can quote file contents and stack traces.
check("generic error event stripped",
      _governance_payload({"name": "builder", "payload_json": json.dumps({"error": SECRET})}) is None)

# Robustness: a governance event with junk or empty payload must not explode.
check("unparseable payload -> stripped",
      _governance_payload({"name": "budget_exceeded", "payload_json": "{not json"}) is None)
check("missing payload -> stripped",
      _governance_payload({"name": "budget_exceeded"}) is None)
check("payload of only unknown keys -> stripped",
      _governance_payload({"name": "budget_exceeded",
                           "payload_json": json.dumps({"prompt": SECRET})}) is None)
check("non-dict payload -> stripped",
      _governance_payload({"name": "budget_exceeded", "payload_json": json.dumps([SECRET])}) is None)

# not_accepted carries the one free-text field allowed through; it must be capped.
long_reason = _governance_payload({
    "name": "not_accepted",
    "payload_json": json.dumps({"reason": "x" * 5000}),
})
check("not_accepted reason truncated", len(json.loads(long_reason or "{}").get("reason", "")) == 300)

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
