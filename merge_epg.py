#!/usr/bin/env python3
"""Fill EPG gaps in guide.xml using epgshare01 country guides.

iptv-org's grabber only covers channels that have a `guides.json` entry. For the
rest, epgshare01 publishes per-country XMLTV files whose channel ids are derived
from the channel *name* (e.g. "Aaj.Tak.in"). We crosswalk those to our
iptv-org channel ids by normalized display-name (preferring the same country),
relabel the epgshare `<channel>`/`<programme>` elements to our tvg-id, and append
them to guide.xml so the whole playlist shares one guide with consistent ids.

Usage: merge_epg.py [guide.xml] [channels.json] [curated_ids.json]
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
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "guide.xml")
CHANNELS = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "data", "channels.json")
CURATED = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "curated_ids.json")

DIR = "https://epgshare01.online/epgshare01/"
BASE = DIR + "epg_ripper_{}.xml.gz"
# iptv-org country code -> epgshare country code (epgshare uses UK, not GB)
CC_MAP = {"UK": "UK", "GB": "UK"}


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\b(hd|fhd|uhd|4k|sd|tv|channel|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def list_files() -> list[str]:
    """All epg_ripper_*.xml.gz filenames listed in the epgshare01 directory."""
    try:
        req = urllib.request.Request(DIR, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "replace")
        return sorted(set(re.findall(r"epg_ripper_([A-Z0-9_]+)\.xml\.gz", html)))
    except Exception as e:
        print(f"  ! directory listing failed: {e}", file=sys.stderr)
        return []


def files_for(cc: str, available: list[str]) -> list[str]:
    """Pick epgshare file keys belonging to a country, e.g. US -> US1/US2/US_LOCALS1."""
    ecc = CC_MAP.get(cc, cc)
    pat = re.compile(rf"^{re.escape(ecc)}(\d|_)")
    return [k for k in available if pat.match(k)]


def fetch(key: str) -> bytes | None:
    url = BASE.format(key)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return gzip.decompress(r.read())
    except Exception as e:
        print(f"  ! {key}: {e}", file=sys.stderr)
        return None


def main():
    if os.path.exists(CHANNELS):
        channels = {c["id"]: c for c in json.load(open(CHANNELS))}
    else:  # git-ignored locally; fetch from the API (e.g. on CI runners)
        print("  channels.json not found; fetching from iptv-org API", file=sys.stderr)
        req = urllib.request.Request("https://iptv-org.github.io/api/channels.json",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            channels = {c["id"]: c for c in json.load(r)}
    curated = set(json.load(open(CURATED)))

    tree = ET.parse(GUIDE)
    root = tree.getroot()
    covered = {c.get("id") for c in root.findall("channel")}

    # uncovered curated channels, indexed by (country, normalized-name) and name
    want_by_cc = defaultdict(dict)   # cc -> {normname: iptv_id}
    want_any = {}                    # normname -> iptv_id (cross-country fallback)
    countries = set()
    for cid in curated:
        if cid in covered:
            continue
        ch = channels.get(cid)
        if not ch:
            continue
        cc = ch.get("country") or ""
        nm = norm(ch.get("name", ""))
        if not nm:
            continue
        want_by_cc[cc][nm] = cid
        want_any.setdefault(nm, cid)
        if cc:
            countries.add(cc)

    total_gap = sum(len(v) for v in want_by_cc.values())
    print(f"EPG merge: {total_gap} uncovered channels across {len(countries)} countries",
          file=sys.stderr)

    available = list_files()
    # every file key we need, mapped back to the iptv-org country it serves
    jobs = []  # (country, filekey)
    for cc in sorted(countries):
        for key in files_for(cc, available):
            jobs.append((cc, key))
    # de-dup file keys (US_SPORTS1 etc. only fetched once) but remember which cc asked
    seen_keys = set()

    filled = {}          # iptv_id -> (source country, epgshare id)
    prog_count = 0
    for cc, key in jobs:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        data = fetch(key)
        if not data:
            continue
        src = ET.parse(io.BytesIO(data)).getroot()
        # map epgshare channel id -> our iptv id (via normalized display-name)
        idmap = {}
        for ch in src.findall("channel"):
            names = [norm(dn.text or "") for dn in ch.findall("display-name")]
            iptv_id = None
            for nm in names:
                if nm in want_by_cc.get(cc, {}):
                    iptv_id = want_by_cc[cc][nm]
                    break
                if nm in want_any:
                    iptv_id = want_any[nm]
            if iptv_id and iptv_id not in filled:
                idmap[ch.get("id")] = iptv_id
                filled[iptv_id] = (key, ch.get("id"))
                # relabel + append the channel element
                ch.set("id", iptv_id)
                root.append(ch)
        # append programmes for matched channels, relabeled
        added = 0
        for pr in src.findall("programme"):
            new = idmap.get(pr.get("channel"))
            if new:
                pr.set("channel", new)
                root.append(pr)
                added += 1
        prog_count += added
        matched = sum(1 for v in filled.values() if v[0] == key)
        print(f"  {key} ({cc}): matched {matched}, +{added} programmes", file=sys.stderr)

    ET.indent(tree, space="  ")
    tree.write(GUIDE, encoding="utf-8", xml_declaration=True)
    print(f"Merged {len(filled)} channels / {prog_count} programmes into guide.xml "
          f"({total_gap - len(filled)} gaps remain)", file=sys.stderr)


if __name__ == "__main__":
    main()
