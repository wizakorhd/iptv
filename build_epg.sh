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

echo "==> Installing epg dependencies (first run only) ..."
( cd "$EPG_DIR" && npm ci --no-audit --no-fund --silent || npm install --no-audit --no-fund --silent )

echo "==> Grabbing guide for curated channels (maxConnections=$MAXCONN, days=$DAYS) ..."
( cd "$EPG_DIR" && npm run grab -- \
    --channels="$HERE/epg.channels.xml" \
    --output="$HERE/guide.xml" \
    --days="$DAYS" \
    --maxConnections="$MAXCONN" )

echo "==> Done. Wrote $HERE/guide.xml"
ls -lh "$HERE/guide.xml"

echo "==> Writing gzipped copy (guide.xml.gz) for faster loading ..."
gzip -9 -k -f "$HERE/guide.xml"
ls -lh "$HERE/guide.xml.gz"
