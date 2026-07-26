#!/usr/bin/env python3
"""Generate docs/channels.json (+ meta) for the GitHub Pages browse/status page.

Parses the built playlist.m3u so the site data always matches what devices load.
EPG availability is derived from guide.xml channel ids.
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST = os.path.join(HERE, "playlist.m3u")
GUIDE = os.path.join(HERE, "guide.xml")
OUT = os.path.join(HERE, "docs", "channels.json")

ATTR = re.compile(r'(\S+?)="(.*?)"')


def epg_ids() -> set:
    if not os.path.exists(GUIDE):
        return set()
    ids = set()
    try:
        for _, el in ET.iterparse(GUIDE, events=("end",)):
            if el.tag == "channel":
                ids.add(el.get("id"))
                el.clear()
    except Exception:
        pass
    return ids


def main():
    have_epg = epg_ids()
    chans = []
    lines = open(PLAYLIST, encoding="utf-8").read().splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXTINF"):
            attrs = dict(ATTR.findall(ln))
            name = ln.split(",", 1)[1] if "," in ln else ""
            # url is the next non-#EXTVLCOPT line
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            url = lines[j] if j < len(lines) else ""
            cid = attrs.get("tvg-id", "")
            # group titles carry a "(N)" count for players like Lume; strip it
            # here since the site renders its own counts.
            group = re.sub(r"\s*\(\d+\)\s*$", "", attrs.get("group-title", ""))
            chans.append({
                "id": cid,
                "name": name,
                "group": group,
                "chno": int(attrs.get("tvg-chno", "0") or 0),
                "logo": attrs.get("tvg-logo", ""),
                "epg": cid in have_epg,
            })
            i = j + 1
        else:
            i += 1

    groups = sorted({c["group"] for c in chans})
    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total": len(chans),
        "with_epg": sum(1 for c in chans if c["epg"]),
        "groups": groups,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"meta": meta, "channels": chans}, open(OUT, "w"), ensure_ascii=False)
    print(f"Wrote {OUT}: {len(chans)} channels, {len(groups)} groups, "
          f"{meta['with_epg']} with EPG", file=sys.stderr)


if __name__ == "__main__":
    main()
