#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Push local traces to Flightbox Cloud.

    uv run adws/cloud_sync.py                 # every run not yet pushed
    uv run adws/cloud_sync.py --all           # every run, re-pushing existing ones
    uv run adws/cloud_sync.py --run <adw_id>  # one run
    uv run adws/cloud_sync.py --status        # what would be sent, and how

Deliberately a separate command, never part of the agent loop: a Cloud outage must not
be able to delay or fail a run. Re-running is safe — ingest upserts on ids the client
already generated, so a repeat push overwrites rather than duplicating.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import cloud  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "adws" / "adw_data" / "flightbox.db"
CONFIG_CANDIDATES = [
    REPO / "adws" / "adw_flightbox_config" / "flightbox.config.yaml",
    REPO / "flightbox.config.yaml",
]


def load_cfg() -> cloud.CloudConfig:
    for p in CONFIG_CANDIDATES:
        if p.exists() and yaml is not None:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return cloud.load_config(data)
    return cloud.CloudConfig()


def main() -> int:
    ap = argparse.ArgumentParser(description="Push local traces to Flightbox Cloud")
    ap.add_argument("--run", help="a single adw_id")
    ap.add_argument("--all", action="store_true", help="re-push every run")
    ap.add_argument("--status", action="store_true", help="show config and recent runs")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    if not DB.exists():
        print(f"no trace db at {DB} — nothing to sync", file=sys.stderr)
        return 1

    cfg = load_cfg()

    if args.status:
        token = cloud.load_token()
        print(f"  mode      {cfg.mode}" + ("" if cfg.enabled else "   (set cloud.sync to enable)"))
        print(f"  url       {cfg.url}")
        print(f"  token     {'present' if token else 'MISSING — set FLIGHTBOX_CLOUD_TOKEN'}")
        print(f"  db        {DB}")
        if cfg.mode == "metadata":
            print("  privacy   prompts and source stay local; costs and row shapes are sent")
        elif cfg.mode == "full":
            print("  privacy   FULL — prompt bodies and diffs will leave this machine")
        print("\n  recent runs:")
        for adw_id, started in cloud.list_runs(DB, args.limit):
            print(f"    {adw_id}  {started or ''}")
        return 0

    if not cfg.enabled:
        print("cloud.sync is off. Set it to `metadata` (or `full`) in flightbox.config.yaml.",
              file=sys.stderr)
        return 1

    token = cloud.load_token()
    if not token:
        print("no token. Set FLIGHTBOX_CLOUD_TOKEN or write ~/.config/flightbox/credentials",
              file=sys.stderr)
        return 1

    runs = [args.run] if args.run else [r for r, _ in cloud.list_runs(DB, args.limit)]
    if not runs:
        print("no runs found")
        return 0

    ok = failed = 0
    for adw_id in runs:
        try:
            res = cloud.push(cfg, token, cloud.build_payload(DB, adw_id, cfg.mode))
            # ASCII only: a Windows cp1252 console raises UnicodeEncodeError on ✔/✘,
            # and this command has to survive CI logs too.
            print(f"  ok   {adw_id}  {res.get('rows_written', '?')} rows  ({res.get('mode')})")
            ok += 1
        except cloud.CloudError as e:
            print(f"  FAIL {adw_id}  {e}", file=sys.stderr)
            failed += 1
            # auth/subscription problems will repeat for every run — stop early
            if "token rejected" in str(e) or "no active subscription" in str(e):
                break
    print(f"\n{ok} synced, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
