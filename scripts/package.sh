#!/usr/bin/env bash
# Build downloadable archives of the Chrome extension and macOS companion.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
mkdir -p "$DIST"
rm -f "$DIST"/*.zip

# ── Chrome extension ────────────────────────────────────────────────────────
EXT_SRC="$ROOT/extension"
EXT_STAGE="$(mktemp -d)/agentwatch-chrome-extension"
mkdir -p "$EXT_STAGE"
# Copy only the files Chrome needs (skip preview_* if anyone left them behind).
rsync -a \
  --exclude '.DS_Store' \
  --exclude 'preview.html' \
  --exclude 'preview_frame.html' \
  --exclude '*.swp' \
  "$EXT_SRC"/ "$EXT_STAGE"/
( cd "$(dirname "$EXT_STAGE")" && zip -qr "$DIST/agentwatch-chrome-extension.zip" "agentwatch-chrome-extension" )

# ── macOS companion ─────────────────────────────────────────────────────────
MAC_SRC="$ROOT/agentwatch-mac"
MAC_STAGE="$(mktemp -d)/agentwatch-mac"
mkdir -p "$MAC_STAGE"
rsync -a \
  --exclude '.DS_Store' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$MAC_SRC"/ "$MAC_STAGE"/
( cd "$(dirname "$MAC_STAGE")" && zip -qr "$DIST/agentwatch-mac.zip" "agentwatch-mac" )

# ── Full bundle (extension + mac app + README) ─────────────────────────────
FULL_STAGE="$(mktemp -d)/AgentWatch"
mkdir -p "$FULL_STAGE"
cp -R "$EXT_STAGE"   "$FULL_STAGE/chrome-extension"
cp -R "$MAC_STAGE"   "$FULL_STAGE/agentwatch-mac"
cp    "$ROOT/README.md" "$FULL_STAGE/README.md"
( cd "$(dirname "$FULL_STAGE")" && zip -qr "$DIST/AgentWatch-full.zip" "AgentWatch" )

echo "Built:"
ls -lh "$DIST"
