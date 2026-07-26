#!/usr/bin/env python3
"""
Generate an iptv-org/epg-compatible channels.xml for the curated playlist.

The output `epg.channels.xml` lists, for every curated channel that has a guide
source, a <channel> line whose `xmltv_id` equals the `tvg-id` used in
playlist.m3u. Feeding this file to the iptv-org/epg grabber therefore yields a
guide.xml whose channel ids line up 1:1 with the playlist (no manual matching).

Usage:
    python3 generate_epg_config.py           # reads curated_ids.json + data/guides.json
Then generate the guide with build_epg.sh (clones iptv-org/epg and runs grab).
"""
import json
import os
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))

# Guide sites to avoid: they crash the grabber (return HTML to a JSON parser)
# or are consistently geo-blocked / 403 from an Indian IP, so they add no data.
BAD_SITES = {
    "tv.mail.ru",   # returns HTML error page -> JSON.parse crash kills whole run
    "tvtv.us",      # 403 from India
    "directv.com",  # 403 from India
}

# One guide site per channel keeps program data clean (no duplicate programmes).
# Prefer these reliable/regional sites (that work from India) when a channel is
# listed on several.
PREFERRED_SITES = [
    "airtelxstream.in", "tatasky.com", "jiotv.com", "siti.in",
    "i.mjh.nz", "sky.com", "ontvtonight.com",
    "gatotv.com", "mi.tv", "programetv.ro", "elcinema.com",
]


def main():
    ids = set(json.load(open(os.path.join(HERE, "curated_ids.json"))))
    guides = json.load(open(os.path.join(HERE, "data", "guides.json")))

    by_channel = {}
    for g in guides:
        cid = g["channel"]
        if cid not in ids:
            continue
        if g.get("site") in BAD_SITES:
            continue
        by_channel.setdefault(cid, []).append(g)

    def rank(g):
        site = g.get("site", "")
        pref = PREFERRED_SITES.index(site) if site in PREFERRED_SITES else 999
        return (pref, 0 if g.get("feed") in (None, "SD") else 1)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<channels>"]
    picked = 0
    for cid in sorted(by_channel):
        g = sorted(by_channel[cid], key=rank)[0]
        site = g.get("site", "")
        site_id = g.get("site_id", "")
        lang = g.get("lang") or "en"
        name = escape(g.get("site_name") or cid)
        lines.append(
            f'  <channel site="{escape(site)}" lang="{escape(lang)}" '
            f'xmltv_id="{escape(cid)}" site_id="{escape(str(site_id))}">'
            f"{name}</channel>"
        )
        picked += 1
    lines.append("</channels>")

    out = os.path.join(HERE, "epg.channels.xml")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    no_guide = len(ids) - len(by_channel)
    print(f"Curated channels ................. {len(ids)}")
    print(f"With an EPG guide source ......... {len(by_channel)}")
    print(f"Without a guide (no EPG data) .... {no_guide}")
    print(f"Wrote {out} ({picked} <channel> entries)")


if __name__ == "__main__":
    main()
