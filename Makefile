# Curated IPTV — Makefile
# Run `make all` locally (from your location) to (re)build everything.

PY ?= python3

.PHONY: all data playlist epg-config epg clean-data clean

all: playlist epg-config epg

## Refresh source metadata from iptv-org
data:
	$(PY) generate_playlist.py --refresh --no-validate --out /dev/null --ids-out /dev/null || true

## Curate + geo-validate streams from THIS machine's location -> playlist.m3u
playlist:
	$(PY) generate_playlist.py

## Same as playlist but skip validation (faster, keeps dead/geo-blocked links)
playlist-fast:
	$(PY) generate_playlist.py --no-validate

## Build epg.channels.xml (iptv-org/epg config) from the curated ids
epg-config:
	$(PY) generate_epg_config.py

## Grab guide.xml (XMLTV EPG) for the curated channels
epg:
	./build_epg.sh 15 2

clean-data:
	rm -rf data

clean: clean-data
	rm -rf .epg epg_build.log
