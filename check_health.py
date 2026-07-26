#!/usr/bin/env python3
"""Re-validate playlist streams and emit a Markdown health report (HEALTH.md).

Intended for the weekly GitHub Action. NOTE: probing runs from the Action
runner's location (usually US), so geo-restricted streams may show as dead here
even though they work from your location — treat this as a signal, not gospel.
"""
import concurrent.futures as cf
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST = os.path.join(HERE, "playlist.m3u")
OUT = os.path.join(HERE, "HEALTH.md")
ATTR = re.compile(r'(\S+?)="(.*?)"')
TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "10"))


def parse():
    lines = open(PLAYLIST, encoding="utf-8").read().splitlines()
    out, i = [], 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            attrs = dict(ATTR.findall(lines[i]))
            name = lines[i].split(",", 1)[1] if "," in lines[i] else ""
            ua = ref = None
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                if lines[j].startswith("#EXTVLCOPT:http-user-agent="):
                    ua = lines[j].split("=", 1)[1]
                elif lines[j].startswith("#EXTVLCOPT:http-referrer="):
                    ref = lines[j].split("=", 1)[1]
                j += 1
            url = lines[j] if j < len(lines) else ""
            out.append({"name": name, "group": attrs.get("group-title", ""),
                        "url": url, "ua": ua, "ref": ref})
            i = j + 1
        else:
            i += 1
    return out


def alive(c) -> bool:
    headers = {"User-Agent": c["ua"] or "Mozilla/5.0", "Range": "bytes=0-2047"}
    if c["ref"]:
        headers["Referer"] = c["ref"]
    try:
        req = urllib.request.Request(c["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status < 400 and bool(r.read(64))
    except Exception:
        return False


def main():
    chans = parse()
    dead = []
    with cf.ThreadPoolExecutor(max_workers=40) as ex:
        for c, ok in zip(chans, ex.map(alive, chans)):
            if not ok:
                dead.append(c)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n, d = len(chans), len(dead)
    lines = [
        f"_Checked {ts} · {n} channels · {n - d} reachable · **{d} unreachable**_",
        "",
        "> Probed from the GitHub runner (US region). Geo-restricted streams may "
        "appear dead here but still work from the intended region — verify before removing.",
        "",
    ]
    if dead:
        from collections import defaultdict
        by = defaultdict(list)
        for c in dead:
            by[c["group"]].append(c["name"])
        lines.append("## Unreachable channels")
        for g in sorted(by):
            lines.append(f"\n### {g} ({len(by[g])})")
            for nm in sorted(by[g]):
                lines.append(f"- {nm}")
    else:
        lines.append("All channels reachable. ✅")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"health: {d}/{n} unreachable -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
