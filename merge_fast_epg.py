#!/usr/bin/env python3
"""Append FAST-service EPG (Samsung TV Plus, Roku, Pluto TV, Plex, PBS, Tubi) for
channels the iptv-org grab + epgshare merge didn't cover.

Two matching strategies are used against each source guide:

  1. exact tvg-id  — Samsung/Roku/Pluto channels whose playlist tvg-id already IS
     the provider channel id (e.g. "IN38000072R"). No relabeling needed.
  2. normalized display-name  — FAST channels ingested from apsattv.com (DistroTV /
     LG / Xiaomi / Vidaa / Tubi / Plex) carry `apsat-*` tvg-ids that don't match any
     provider guide. We crosswalk them by normalized channel *name* to a source
     <channel> and relabel that source's <channel>/<programme> ids to our tvg-id.

Sources are matthuisman's per-provider aggregate XMLTV (i.mjh.nz) plus a prebuilt
Tubi guide. Using the "all" aggregates (rather than a few regions) maximizes the
name-crosswalk hit rate for the international FAST catalog.

Usage: merge_fast_epg.py [guide.xml] [curated_ids.json] [playlist.m3u]
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "guide.xml")
CURATED = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "curated_ids.json")
PLAYLIST = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "playlist.m3u")

# Prebuilt per-provider XMLTV. The "all" aggregates cover far more of the
# international FAST catalog than the handful of country files we used before,
# which is what the name-crosswalk needs to lift coverage.
SOURCES = [
    ("samsung", "https://i.mjh.nz/SamsungTVPlus/all.xml.gz"),
    ("pluto",   "https://i.mjh.nz/PlutoTV/all.xml.gz"),
    ("plex",    "https://i.mjh.nz/Plex/all.xml.gz"),
    ("roku",    "https://i.mjh.nz/Roku/all.xml.gz"),
    ("pbs",     "https://i.mjh.nz/PBS/all.xml.gz"),
    ("tubi",    "https://raw.githubusercontent.com/BuddyChewChew/"
                "app-m3u-generator/main/playlists/tubi_epg.xml"),
]

# Generic one-word names that collide across unrelated channels; skip name-only
# matches for these to avoid mislabeling (exact-id matches are still allowed).
GENERIC = {
    "movies", "news", "kids", "sports", "music", "comedy", "drama", "action",
    "cinema", "classic", "classics", "family", "live", "local", "world",
    "entertainment", "documentary", "documentaries", "reality", "horror",
    "crime", "food", "travel", "series", "general", "cartoons", "anime",
}


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(hd|fhd|uhd|4k|sd|tv|channel|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def fetch(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        return gzip.decompress(data) if url.endswith(".gz") else data
    except Exception as e:
        print(f"  ! {url}: {e}", file=sys.stderr)
        return None


def playlist_names(path: str) -> dict[str, str]:
    """tvg-id -> channel display name, parsed from the M3U EXTINF lines."""
    names: dict[str, str] = {}
    if not os.path.exists(path):
        print(f"  ! playlist not found ({path}); name-crosswalk disabled",
              file=sys.stderr)
        return names
    for line in open(path, encoding="utf-8"):
        if not line.startswith("#EXTINF"):
            continue
        tid = (re.search(r'tvg-id="([^"]*)"', line) or [None, ""])[1]
        disp = line.split(",", 1)[1].strip() if "," in line else ""
        nm = (re.search(r'tvg-name="([^"]*)"', line) or [None, ""])[1] or disp
        if tid and nm:
            names[tid] = nm
    return names


def main():
    curated = set(json.load(open(CURATED)))
    id_name = playlist_names(PLAYLIST)

    tree = ET.parse(GUIDE)
    root = tree.getroot()
    covered = {c.get("id") for c in root.findall("channel")}
    want = curated - covered

    # Name index for the uncovered channels: normalized-name -> our tvg-id.
    want_by_name: dict[str, str] = {}
    for cid in want:
        nm = norm(id_name.get(cid, ""))
        if nm and nm not in GENERIC:
            want_by_name.setdefault(nm, cid)

    added_ch = prog = 0
    got: set[str] = set()          # our tvg-ids already filled this run
    for label, url in SOURCES:
        data = fetch(url)
        if not data:
            continue
        try:
            src = ET.parse(io.BytesIO(data)).getroot()
        except Exception as e:
            print(f"  ! {label}: parse failed: {e}", file=sys.stderr)
            continue

        idmap: dict[str, str] = {}   # source channel id -> our tvg-id
        exact = byname = 0
        for ch in src.findall("channel"):
            cid = ch.get("id")
            if not cid or cid in got:
                continue
            # 1) exact tvg-id match (Samsung/Roku/Pluto native ids)
            if cid in want:
                idmap[cid] = cid
                got.add(cid)
                root.append(ch)
                exact += 1
                added_ch += 1
                continue
            # 2) normalized display-name crosswalk (apsat-* ids)
            target = None
            for dn in ch.findall("display-name"):
                target = want_by_name.get(norm(dn.text or ""))
                if target:
                    break
            if target and target not in got:
                idmap[cid] = target
                got.add(target)
                ch.set("id", target)
                root.append(ch)
                byname += 1
                added_ch += 1

        added = 0
        for pr in src.findall("programme"):
            new = idmap.get(pr.get("channel"))
            if new:
                pr.set("channel", new)
                root.append(pr)
                added += 1
        prog += added
        print(f"  {label}: +{exact} by-id, +{byname} by-name, +{added} programmes",
              file=sys.stderr)

    ET.indent(tree, space="  ")
    tree.write(GUIDE, encoding="utf-8", xml_declaration=True)
    print(f"Merged {added_ch} FAST channels / {prog} programmes into guide.xml "
          f"({len(want) - added_ch} FAST gaps remain)", file=sys.stderr)


if __name__ == "__main__":
    main()
