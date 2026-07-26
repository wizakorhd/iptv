#!/usr/bin/env bash
#
# Unattended build+publish for the curated IPTV playlist.
# Invoked by launchd (com.wizakorhd.iptv-build) on a schedule, from THIS Mac
# (in India) so stream geo-validation matches the target audience.
#
# It: pulls latest -> rebuilds playlist + EPG + site -> commits -> pushes.
# All output is appended to logs/auto-build.log (rotated at ~5 MB).
#
set -uo pipefail

REPO="/Users/arwen/Development/legolas/iptv"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/auto-build.log"

# launchd gives us a minimal PATH; add Homebrew, nvm node, and system dirs.
NODE_BIN="$(/bin/ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | /usr/bin/sort -V | /usr/bin/tail -1)"
export PATH="$NODE_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/arwen}"

mkdir -p "$LOG_DIR"
# rotate if big
if [ -f "$LOG" ] && [ "$(/usr/bin/stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  /bin/mv "$LOG" "$LOG.1"
fi

exec >>"$LOG" 2>&1
echo "======================================================================"
echo "==> Build started: $(date '+%Y-%m-%d %H:%M:%S %Z')"

cd "$REPO" || { echo "!! repo not found: $REPO"; exit 1; }

FORCE="${1:-}"
MARKER="$LOG_DIR/.last-success"
MIN_SECONDS=$((20 * 3600))   # build at most once per ~day, but retry if a run was skipped/failed

# Skip if we already had a successful build recently (unless --force).
if [ "$FORCE" != "--force" ] && [ -f "$MARKER" ]; then
  LAST="$(cat "$MARKER" 2>/dev/null || echo 0)"
  NOW="$(date +%s)"
  if [ $((NOW - LAST)) -lt "$MIN_SECONDS" ]; then
    HRS=$(( (NOW - LAST) / 3600 ))
    echo "==> last successful build was ${HRS}h ago (<20h); skipping this attempt"
    exit 0
  fi
fi

# Only run on a real network (avoid churning while offline / captive portal).
# NOTE: we do NOT update the success marker here, so the next scheduled
# attempt will retry rather than waiting a full day.
if ! /usr/bin/curl -fsS -m 15 -o /dev/null https://raw.githubusercontent.com; then
  echo "!! no network to GitHub raw; skipping this run (will retry next attempt)"
  exit 0
fi

echo "==> git pull --ff-only"
git pull --ff-only origin main || echo "!! pull failed (continuing with local state)"

echo "==> make all (playlist + epg + site)"
if ! make all; then
  echo "!! build failed; leaving repo untouched"
  exit 1
fi

if git diff --quiet && git diff --cached --quiet; then
  echo "==> no changes after rebuild; nothing to commit"
  date +%s > "$MARKER"
  echo "==> Build finished (no-op): $(date '+%H:%M:%S')"
  exit 0
fi

STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
N="$(grep -c '^#EXTINF' playlist.m3u 2>/dev/null || echo '?')"
git add -A
git commit -q -m "Auto rebuild ${STAMP} (${N} channels, geo-validated from India)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
echo "==> committed: $(git log --oneline -1)"

echo "==> git push"
if git push origin main; then
  echo "==> pushed OK"
else
  echo "!! push failed (auth?). Commit is saved locally; will retry next run."
fi

echo "==> Build finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"
date +%s > "$MARKER"

# ---- Housekeeping: keep the working tree and caches from growing unbounded ----
echo "==> Housekeeping"
# Compact git object stores (main repo + the shallow iptv-org/epg cache).
git gc --auto --quiet 2>/dev/null || true
[ -d .epg/.git ] && git -C .epg gc --auto --quiet 2>/dev/null || true
# Drop the bulky uncompressed guide from disk (we publish only guide.xml.gz).
[ -f guide.xml ] && rm -f guide.xml
# Trim rotated logs to a single generation.
rm -f "$LOG_DIR"/*.1 2>/dev/null || true
# Remove any stray temp files this project may leave behind.
rm -f epg_build.log refresh.log 2>/dev/null || true
echo "==> Housekeeping done"
