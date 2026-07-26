#!/usr/bin/env bash
#
# Build guide.xml (XMLTV EPG) for the curated channels using iptv-org/epg.
# The generated guide's channel ids match tvg-id in playlist.m3u exactly.
#
# Usage:  ./build_epg.sh [maxConnections] [days]
#   maxConnections  parallel site connections (default 10)
#   days            days of guide to fetch (default 2)
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EPG_DIR="$HERE/.epg"                 # cloned iptv-org/epg lives here (git-ignored)
MAXCONN="${1:-10}"
DAYS="${2:-3}"

command -v node >/dev/null || { echo "node is required (nvm: 'nvm use 20')"; exit 1; }

if [ ! -d "$EPG_DIR/.git" ]; then
  echo "==> Cloning iptv-org/epg (shallow) ..."
  git clone --depth 1 https://github.com/iptv-org/epg.git "$EPG_DIR"
fi

# Install deps only when missing (node_modules absent or package.json changed).
# `npm ci` on every run wasted minutes; this makes repeat builds much faster.
STAMP="$EPG_DIR/node_modules/.iptv-deps-stamp"
if [ ! -d "$EPG_DIR/node_modules" ] || [ "$EPG_DIR/package-lock.json" -nt "$STAMP" ]; then
  echo "==> Installing epg dependencies ..."
  ( cd "$EPG_DIR" && npm ci --no-audit --no-fund --silent || npm install --no-audit --no-fund --silent )
  touch "$STAMP"
else
  echo "==> epg dependencies present; skipping install"
fi

echo "==> Grabbing guide for curated channels (maxConnections=$MAXCONN, days=$DAYS) ..."
# Keep the log readable: drop the ~2000 per-channel progress lines but keep any
# errors and the final summary. Preserve the grabber's real exit code (pipefail).
set +e
( cd "$EPG_DIR" && npm run grab -- \
    --channels="$HERE/epg.channels.xml" \
    --output="$HERE/guide.xml" \
    --days="$DAYS" \
    --maxConnections="$MAXCONN" ) 2>&1 \
  | grep -vE '^ℹ[[:space:]]+\[[0-9]+/[0-9]+\]'
GRAB_RC=${PIPESTATUS[0]}
set -e
if [ "$GRAB_RC" -ne 0 ]; then
  echo "!! grab exited with code $GRAB_RC"
  exit "$GRAB_RC"
fi

echo "==> Done. Wrote $HERE/guide.xml"
ls -lh "$HERE/guide.xml"

echo "==> Filling EPG gaps from epgshare01 (name crosswalk) ..."
python3 "$HERE/merge_epg.py" "$HERE/guide.xml" || echo "  (merge skipped/failed; keeping iptv-org guide)"

echo "==> Writing gzipped copy (guide.xml.gz) for faster loading ..."
gzip -9 -k -f "$HERE/guide.xml"
ls -lh "$HERE/guide.xml.gz"
