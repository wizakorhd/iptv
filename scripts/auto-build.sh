#!/usr/bin/env bash
#
# Unattended build+publish for the curated IPTV playlist.
# Run on a schedule from a connection in the target region so stream
# geo-validation matches the intended audience.
#
# It: pulls latest -> rebuilds playlist + EPG config + site -> commits -> pushes.
# All output is appended to logs/auto-build.log (rotated at ~5 MB).
#
set -uo pipefail

# Resolve the repo root from this script's location (scripts/ is one level down).
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/auto-build.log"

# A scheduler may hand us a minimal PATH; add common node + system dirs.
NODE_BIN="$(/bin/ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | /usr/bin/sort -V | /usr/bin/tail -1)"
export PATH="$NODE_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$LOG_DIR"
# rotate if big
if [ -f "$LOG" ] && [ "$(/usr/bin/stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  /bin/mv "$LOG" "$LOG.1"
fi

exec >>"$LOG" 2>&1
echo "======================================================================"
echo "==> Build started: $(date '+%Y-%m-%d %H:%M:%S %Z')"

cd "$REPO" || { echo "!! repo not found: $REPO"; exit 1; }

# macOS toast notification (best-effort). Runs from a LaunchAgent, so it has a
# GUI session; a no-op on headless/non-mac boxes. notify "title" "message".
notify() {
  command -v osascript >/dev/null 2>&1 || return 0
  /usr/bin/osascript -e "display notification \"${2//\"/\'}\" with title \"${1//\"/\'}\"" >/dev/null 2>&1 || true
}

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
notify "IPTV build" "Rebuilding playlist…"
git pull --ff-only origin main || echo "!! pull failed (continuing with local state)"

# --- Refresh the iptv-org source snapshot weekly so newly added / removed
#     channels + streams are picked up (a plain build reuses the cached data/). ---
DATA_MAX_AGE=$((7 * 24 * 3600))
REFRESH=""
if [ ! -f data/streams.json ]; then
  REFRESH="--refresh --no-cache"
else
  DAGE=$(( $(date +%s) - $(stat -f %m data/streams.json) ))
  [ "$DAGE" -ge "$DATA_MAX_AGE" ] && REFRESH="--refresh --no-cache"
fi
echo "==> Rebuild playlist (geo-validated)${REFRESH:+ [refreshing source data]}"
if ! make playlist REFRESH="$REFRESH"; then
  echo "!! playlist build failed; leaving repo untouched"
  notify "IPTV build ✗" "Playlist build failed"
  exit 1
fi
# Regenerate the EPG channel list so the GitHub Actions EPG builder grabs the
# current curated set. The EPG *grab* itself runs on GitHub (location-independent)
# so this build never clones iptv-org/epg or its node_modules — see README.
make epg-config || { echo "!! epg-config failed"; exit 1; }

echo "==> Regenerate site data"
# with_epg is read from the committed guide.xml.gz (built by the GitHub Action);
# it may lag the very latest EPG by one build, which is fine.
make site || echo "!! site generation failed (continuing)"

if git diff --quiet && git diff --cached --quiet; then
  echo "==> no changes after rebuild; nothing to commit"
  date +%s > "$MARKER"
  echo "==> Build finished (no-op): $(date '+%H:%M:%S')"
  notify "IPTV build ✓" "No changes — already up to date"
  exit 0
fi

STAMP="$(date '+%Y-%m-%d %H:%M %Z')"
N="$(grep -c '^#EXTINF' playlist.m3u 2>/dev/null || echo '?')"
git add -A
git commit -q -m "Auto rebuild ${STAMP} (${N} channels)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
echo "==> committed: $(git log --oneline -1)"

echo "==> git push"
# The GitHub EPG Action may push guide.xml.gz around the same time. We touch
# disjoint files (it owns guide.xml.gz; we own playlists/ids/site), so a rejected
# push just needs a rebase + retry with no conflicts.
pushed=""
for attempt in 1 2 3; do
  if git push origin main; then pushed="yes"; break; fi
  echo "   push rejected (attempt $attempt); rebasing on origin/main and retrying"
  git fetch --quiet origin main && git rebase --quiet origin/main || { git rebase --abort 2>/dev/null; break; }
done
if [ -n "$pushed" ]; then
  echo "==> pushed OK"
  notify "IPTV build ✓" "Published ${N} channels"
else
  echo "!! push failed. Commit is saved locally; will retry next run."
  notify "IPTV build ⚠︎" "Built ${N} channels — push failed, will retry"
fi

echo "==> Build finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"
date +%s > "$MARKER"

# ---- Housekeeping: keep the working tree and caches from growing unbounded ----
echo "==> Housekeeping"
# Compact the git object store.
git gc --auto --quiet 2>/dev/null || true
# The EPG grab runs on GitHub, so there should be no local iptv-org/epg clone;
# remove it if an older build left one behind (frees ~460 MB).
[ -d .epg ] && rm -rf .epg
# Drop any bulky uncompressed guide from disk (we publish only guide.xml.gz).
[ -f guide.xml ] && rm -f guide.xml
# Trim rotated logs to a single generation.
rm -f "$LOG_DIR"/*.1 2>/dev/null || true
# Remove any stray temp files this project may leave behind.
rm -f epg_build.log refresh.log .grab.out 2>/dev/null || true
echo "==> Housekeeping done"
