# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""What a stamped repo ships with, and who the halt talks to.

Two things worth a test of their own:

  1. The config we stamp into a fresh repo must actually carry governance. The
     caps used to ship commented out, which meant a new install behaved exactly
     like no flight recorder at all until someone remembered to configure one.

  2. The `cloud:` block must survive the trip through pydantic. It used to be
     dropped silently (FLIGHTBOXConfig had no field for it), so a paying
     customer's `sync: metadata` vanished and the post-halt line still told them
     to go buy the thing they had already bought. cloud_sync.py never noticed,
     because it parses the yaml itself: two readers, one of them wrong.

Run:  uv run tests/test_cloud_config.py
"""

import sys
from pathlib import Path

import yaml

TPL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "flightbox" / "templates"
sys.path.insert(0, str(TPL / "adws"))

from adw_modules.data_types import FLIGHTBOXConfig  # noqa: E402


def nags(yaml_text: str) -> bool:
    """Mirror Run._mention_cloud's decision for a given config."""
    cfg = FLIGHTBOXConfig(**(yaml.safe_load(yaml_text) or {}))
    return getattr(getattr(cfg, "cloud", None), "sync", None) not in ("metadata", "full")


# (label, yaml, should the halt mention Cloud?)
CASES = [
    ("no cloud block at all",              "defaults: {}",                                    True),
    ("sync: off",                          "cloud:\n  sync: off",                             True),
    ('sync: "off" (quoted)',               'cloud:\n  sync: "off"',                           True),
    ("sync: no  (yaml 1.1 -> False)",      "cloud:\n  sync: no",                              True),
    ("sync: metadata  (paying)",           "cloud:\n  sync: metadata",                        False),
    ("sync: full  (paying)",               "cloud:\n  sync: full",                            False),
    ("sync: MetaData  (case)",             "cloud:\n  sync: MetaData",                        False),
    ("paying + custom url",                "cloud:\n  sync: metadata\n  url: https://x.dev",  False),
]

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"[{'PASS' if got == want else 'FAIL'}] {label}")


for label, text, want in CASES:
    # ASCII only: this repo has been bitten twice by a Windows cp1252 console
    # choking on a non-ascii glyph, and a test that dies printing its own name
    # is worse than no test.
    check(f"halt mentions Cloud - {label}", nags(text), want)

# `on`/`yes`/`true` must be REFUSED, not guessed at. yaml hands us True having
# thrown away which word was written, and the user never said whether they meant
# metadata (costs only) or full (prompts and source too). Guessing "metadata"
# would start transmitting to a remote server on a spelling nobody documented.
for spelling in ("on", "yes", "true", "True"):
    try:
        FLIGHTBOXConfig(**yaml.safe_load(f"cloud:\n  sync: {spelling}"))
        check(f"sync: {spelling} refused rather than guessed", False, True)
    except Exception:
        check(f"sync: {spelling} refused rather than guessed", True, True)

# The run and the sync CLI must never disagree about what a config means. They
# did once: the run saw a paying customer, the CLI hard-failed on every push.
from adw_modules import cloud  # noqa: E402

for text in ("cloud:\n  sync: off", "cloud:\n  sync: no", "cloud:\n  sync: metadata",
             "cloud:\n  sync: FULL", 'cloud:\n  sync: "metadata "', "defaults: {}"):
    raw = yaml.safe_load(text)
    via_run = FLIGHTBOXConfig(**raw).cloud.sync
    via_cli = cloud.load_config(raw).mode
    check(f"both readers agree on {text.splitlines()[-1].strip()!r}", via_run, via_cli)

for text in ("cloud:\n  sync: on", "cloud:\n  sync: metadta"):
    raw = yaml.safe_load(text)
    run_failed = cli_failed = False
    try:
        FLIGHTBOXConfig(**raw)
    except Exception:
        run_failed = True
    try:
        cloud.load_config(raw)
    except Exception:
        cli_failed = True
    check(f"both readers reject {text.splitlines()[-1].strip()!r}", (run_failed, cli_failed), (True, True))

# The config we actually stamp into a fresh repo.
shipped = FLIGHTBOXConfig(**yaml.safe_load((TPL / "flightbox.config.yaml").read_text("utf-8")))
check("stamped config caps the run", shipped.defaults.max_run_cost, 25.00)
check("stamped config sets a month budget", shipped.defaults.month_budget, 100.00)
check("stamped config leaves sync off", shipped.cloud.sync, "off")
check("stamped config has agents", bool(shipped.agents), True)

# A typo must fail loudly at load. Silently falling back to "off" would mean
# someone believing they were syncing while nothing ever left the machine.
try:
    FLIGHTBOXConfig(**yaml.safe_load("cloud:\n  sync: metadta"))
    check("typo'd sync mode rejected", False, True)
except Exception:
    check("typo'd sync mode rejected", True, True)

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
