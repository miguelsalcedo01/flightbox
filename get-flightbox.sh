#!/bin/sh
# Flightbox bootstrapper — run from the ROOT of the repo you want governed:
#
#   curl -fsSL https://raw.githubusercontent.com/miguelsalcedo01/flightbox/main/get-flightbox.sh | sh
#
# Fetches the flightbox skill, stamps the factory into the current repo, and
# preps .env. Idempotent: existing files are skipped, never overwritten.
# Override the source with FLIGHTBOX_REPO (a URL or a local checkout path).
set -e

REPO="${FLIGHTBOX_REPO:-https://github.com/miguelsalcedo01/flightbox}"

command -v git >/dev/null 2>&1 || { echo "error: git is required"; exit 1; }
command -v uv  >/dev/null 2>&1 || { echo "error: uv is required — https://docs.astral.sh/uv/"; exit 1; }

if [ -d "$REPO/.claude/skills/flightbox" ]; then
    SRC="$REPO"                                  # local checkout, no network
else
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    echo "fetching flightbox from $REPO ..."
    git clone --quiet --depth 1 "$REPO" "$TMP/flightbox"
    SRC="$TMP/flightbox"
fi

mkdir -p .claude/skills
rm -rf .claude/skills/flightbox
# tar, not cp -r: a used checkout has node_modules/ and dist/ inside the
# visualizer (thousands of files) — build artifacts, not the product.
(cd "$SRC/.claude/skills" && tar -cf - \
    --exclude 'node_modules' --exclude 'dist' --exclude '__pycache__' \
    flightbox) | (cd .claude/skills && tar -xf -)

uv run .claude/skills/flightbox/scripts/install.py

if [ ! -f .env ] && [ -f .env.sample ]; then
    cp .env.sample .env
    echo "created .env — set your provider API keys in it"
fi

echo ""
echo "flightbox is stamped. next:"
echo "  1. edit .env (API keys)"
echo "  2. just demo          # two cheap read-only runs, end to end"
echo "  3. just obs           # the replay UI (needs bun)"
