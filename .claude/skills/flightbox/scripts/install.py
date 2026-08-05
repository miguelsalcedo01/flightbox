#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the FLIGHTBOX factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--force]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(4 starter agents), adws/adw_flightbox_config/flightbox.config.yaml, .env.sample,
.gitignore entries.
Existing files are skipped unless --force.
"""

import argparse
import shutil
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

GITIGNORE_ENTRIES = [
    "adws/adw_data/sessions/",
    "adws/adw_data/flightbox.db*",
    ".env",
    # The ADWs are Python, so importing adw_modules writes bytecode next to it.
    # Chains that end in a commit phase call `git add -A`, so without this a
    # stamped repo commits its own .pyc files — 15 of them showed up in the
    # first repo that was ever installed into from scratch.
    "__pycache__/",
    "*.pyc",
]


def stamp(src: Path, dest: Path, force: bool, stamped: list, skipped: list) -> None:
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name == "__pycache__":
                continue
            stamp(child, dest / child.name, force, stamped, skipped)
        return
    if dest.exists() and not force:
        skipped.append(str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    stamped.append(str(dest))


def ensure_gitignore(root: Path, stamped: list) -> None:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# flightbox runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument(
        "--upgrade", action="store_true",
        help="refresh the engine (adws/adw_modules/) but leave your config, prompts "
             "and data alone — how you pick up fixes without re-answering setup")
    args = parser.parse_args()

    root = Path.cwd()
    stamped, skipped = [], []

    # An install that only ever skips existing files can never deliver a fix. That
    # is not hypothetical: the `cloud:` block was silently dropped by an old
    # data_types.py, and the people who most needed the fix — paying customers —
    # were exactly the ones who already had the file and so never received it.
    # Their only route was --force, which also overwrites the config they had
    # tuned.
    #
    # --upgrade refreshes the engine only. It is deliberately NOT the default and
    # NOT unconditional: adw_modules/ is where a fork's local changes live (a
    # customized quality.py is the common one), and silently reverting someone's
    # edits during an unrelated install would be a worse bug than the one this
    # fixes. Opt in, then read `git diff` — that is the safety net, so upgrade on
    # a clean tree.
    if args.upgrade:
        upgraded: list = []
        stamp(TEMPLATES / "adws" / "adw_modules", root / "adws" / "adw_modules",
              True, upgraded, [])
        if upgraded:
            print(f"upgraded engine ({len(upgraded)} files in adws/adw_modules/)")
            print("  review with: git diff adws/adw_modules/")
            print("  local changes there (e.g. a customized quality.py) were overwritten.")
        stamped.extend(upgraded)

    stamp(TEMPLATES / "adws", root / "adws", args.force, stamped, skipped)
    stamp(TEMPLATES / "prompt_engineering",
          root / "adws" / "adw_data" / "prompt_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "harness_engineering",
          root / "adws" / "adw_data" / "harness_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "flightbox.config.yaml",
          root / "adws" / "adw_flightbox_config" / "flightbox.config.yaml",
          args.force, stamped, skipped)
    stamp(TEMPLATES / "env.sample", root / ".env.sample", args.force, stamped, skipped)
    # The recipes are part of the operating experience, and several cookbooks
    # plus the run banner tell you to use them, so a stamped repo has to have
    # them. Skipped like any other file if the repo already has a justfile.
    stamp(TEMPLATES / "justfile", root / "justfile", args.force, stamped, skipped)
    ensure_gitignore(root, stamped)

    print(f"flightbox installed into {root}")
    print(f"  stamped: {len(stamped)} file(s)")
    for s in stamped:
        print(f"    + {s}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")
    print("\nnext steps:")
    print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
    print("  2. just demo             # two cheap read-only runs, end to end")
    print("  3. just sessions         # what just happened")
    print("  4. just obs              # the trace UI, needs bun")
    print("\n  no just? the raw form of step 2 is:")
    print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
