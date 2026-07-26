#!/usr/bin/env python3
"""Append Samsung TV Plus EPG (from i.mjh.nz) for FAST channels in the playlist.

generate_playlist.py adds Samsung TV Plus channels whose tvg-id is the Samsung
channel id (e.g. "IN38000072R"). Their guide isn't produced by the iptv-org grab,
so we pull matthuisman's per-region Samsung XMLTV and append the
<channel>/<programme> elements whose id is one of our curated ids. No relabeling
is needed: the ids already match the playlist's tvg-id.

Usage: merge_fast_epg.py [guide.xml] [curated_ids.json]
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "guide.xml")
CURATED = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "curated_ids.json")

# Same regions generate_playlist.py pulls Samsung channels from.
REGIONS = ("in", "us", "gb", "ca")
URL = "https://i.mjh.nz/SamsungTVPlus/{region}.xml.gz"


def fetch(region: str) -> bytes | None:
    try:
        req = urllib.request.Request(URL.format(region=region),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return gzip.decompress(r.read())
    except Exception as e:
        print(f"  ! samsung {region}: {e}", file=sys.stderr)
        return None


def main():
    curated = set(json.load(open(CURATED)))
    tree = ET.parse(GUIDE)
    root = tree.getroot()
    covered = {c.get("id") for c in root.findall("channel")}
    want = curated - covered

    added_ch = prog = 0
    got: set[str] = set()
    for reg in REGIONS:
        data = fetch(reg)
        if not data:
            continue
        src = ET.parse(io.BytesIO(data)).getroot()
        ids = set()
        for ch in src.findall("channel"):
            cid = ch.get("id")
            if cid in want and cid not in got:
                root.append(ch)
                got.add(cid)
                ids.add(cid)
                added_ch += 1
        for pr in src.findall("programme"):
            if pr.get("channel") in ids:
                root.append(pr)
                prog += 1
        print(f"  samsung {reg}: +{len(ids)} channels", file=sys.stderr)

    ET.indent(tree, space="  ")
    tree.write(GUIDE, encoding="utf-8", xml_declaration=True)
    print(f"Merged {added_ch} FAST channels / {prog} programmes into guide.xml "
          f"({len(want) - added_ch} FAST gaps remain)", file=sys.stderr)


if __name__ == "__main__":
    main()
