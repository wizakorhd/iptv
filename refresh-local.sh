#!/usr/bin/env bash
#
# Local daily refresh: rebuild the geo-validated playlist FROM THIS LOCATION,
# refresh the EPG, and push to GitHub. Meant to be run by launchd (see
# com.wizakorhd.iptv.refresh.plist) so the "no-VPN from India" filtering stays
# correct (a US-based GitHub runner cannot do this).
#
# Auth: uses git's osxkeychain credential helper. Store the token once with:
#   git config --global credential.helper osxkeychain
#   printf 'protocol=https\nhost=github.com\nusername=x-access-token\npassword=<PAT>\n' \
#     | git credential-osxkeychain store
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# node for the EPG grab
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh"; nvm use 20 >/dev/null 2>&1 || true; fi

echo "[$(date)] refreshing source data + playlist ..."
python3 generate_playlist.py --refresh
python3 generate_epg_config.py
./build_epg.sh 15 3

echo "[$(date)] committing + pushing ..."
git add playlist.m3u playlist-*.m3u curated_ids.json epg.channels.xml \
        guide.xml guide.xml.gz REPORT.md
if ! git diff --cached --quiet; then
  git commit -m "chore: local refresh ($(date -u +%Y-%m-%d))"
  git push
  echo "[$(date)] pushed."
else
  echo "[$(date)] nothing changed."
fi
