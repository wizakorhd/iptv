# Curated IPTV — Makefile
# Run `make all` locally (from your location) to (re)build everything.

PY ?= python3

.PHONY: all data playlist playlist-fast epg-config epg site health clean-data clean

all: playlist epg-config epg site

## Refresh source metadata from iptv-org
data:
	$(PY) generate_playlist.py --refresh --no-validate --out /dev/null --ids-out /dev/null || true

## Curate + geo-validate streams from the build machine's location -> playlist.m3u
## Pass REFRESH=--refresh to also re-download the iptv-org source snapshot.
playlist:
	$(PY) generate_playlist.py $(REFRESH)

## Same as playlist but skip validation (faster, keeps dead/geo-blocked links)
playlist-fast:
	$(PY) generate_playlist.py --no-validate

## Build epg.channels.xml (iptv-org/epg config) from the curated ids
epg-config:
	$(PY) generate_epg_config.py

## Grab guide.xml (XMLTV EPG) for the curated channels (+ epgshare01 gap fill)
epg:
	./build_epg.sh 20 3

## Generate the GitHub Pages browse/status data (docs/channels.json)
site:
	$(PY) gen_site.py

## Re-validate streams and write health-report.md (US-location caveat)
health:
	$(PY) check_health.py

clean-data:
	rm -rf data

clean: clean-data
	rm -rf .epg epg_build.log

## Reclaim all reusable caches (source data + iptv-org/epg clone + node_modules).
## Next build re-downloads/re-clones/re-installs (slower), so use sparingly.
deepclean: clean
	rm -f guide.xml .grab.out refresh.log health-report.md
	@echo "Caches removed. Next build will be a cold start."
