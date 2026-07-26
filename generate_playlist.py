#!/usr/bin/env python3
"""
Curated IPTV playlist generator (built on the free, community iptv-org dataset).

Curation rules
--------------
  * Hindi channels ......... all categories (language contains 'hin')
  * English Indian ......... all categories (country == IN and language contains 'eng')
  * English foreign ........ categories in {movies, entertainment, sports, news}
                             (language contains 'eng', country != IN)

For the "no VPN required" requirement, every stream is probed *from this machine's
location*. Streams that are dead or geo-blocked from here are dropped, so the final
playlist only contains channels that actually play for you without a VPN.

Data source: https://iptv-org.github.io/api/  (see data/ for cached copies)
Attribution: iptv-org (MIT). This tool only re-arranges public metadata.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict

API = "https://iptv-org.github.io/api"
FILES = ["channels", "streams", "feeds", "categories", "languages",
         "countries", "blocklist", "logos"]
FOREIGN_CATS = {"movies", "entertainment", "sports", "news"}
# Anime is not a category in the dataset, so detect dedicated anime channels by name.
ANIME_KW = ("anime", "animax", "ani-one", "ani one", "aniplus", "toonami",
            "crunchyroll", "one piece", "naruto", "pokemon", "dragon ball",
            "gundam", "hidive", "filmrise anime", "kanade")
# We accept anime as sub or dub, so English- or Japanese-audio anime only
# (skips Spanish/Portuguese/German-only anime feeds).
ANIME_LANGS = {"eng", "jpn", "jap"}
QUALITY_RANK = {"2160p": 6, "1440p": 5, "1080p": 4, "720p": 3,
                "576p": 2, "480p": 1, "360p": 0, "240p": 0}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")


# ----------------------------------------------------------------------------- data
def load(refresh: bool) -> dict:
    os.makedirs(DATA, exist_ok=True)
    out = {}
    for name in FILES:
        path = os.path.join(DATA, f"{name}.json")
        if refresh or not os.path.exists(path):
            print(f"  downloading {name}.json ...", file=sys.stderr)
            req = urllib.request.Request(f"{API}/{name}.json",
                                         headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                open(path, "wb").write(r.read())
        out[name] = json.load(open(path))
    return out


def quality_score(q: str | None) -> int:
    return QUALITY_RANK.get((q or "").lower(), -1)


def primary_group(prefix: str, cats: set[str]) -> str:
    for c in ("movies", "sports", "news", "entertainment", "kids",
              "music", "documentary", "series"):
        if c in cats:
            return f"{prefix} - {c.capitalize()}"
    return f"{prefix} - General"


# ------------------------------------------------------------------------- curation
def build_candidates(db: dict) -> list[dict]:
    channels = {c["id"]: c for c in db["channels"]}
    block = {b["channel"] for b in db["blocklist"]}

    # languages live on feeds now -> collapse to per-channel language set,
    # and keep the ORDERED languages of the main feed (first = primary).
    ch_langs: dict[str, set] = defaultdict(set)
    main_feed: dict[str, str] = {}
    main_feed_langs: dict[str, list] = {}
    for f in db["feeds"]:
        cid = f["channel"]
        flangs = f.get("languages") or []
        for lang in flangs:
            ch_langs[cid].add(lang)
        if f.get("is_main") or cid not in main_feed_langs:
            if f.get("is_main"):
                main_feed[cid] = f["id"]
            main_feed_langs.setdefault(cid, flangs)
            if f.get("is_main"):
                main_feed_langs[cid] = flangs

    # streams grouped by channel
    ch_streams: dict[str, list] = defaultdict(list)
    for s in db["streams"]:
        if s.get("channel"):
            ch_streams[s["channel"]].append(s)

    # best in-use logo per channel
    ch_logo: dict[str, str] = {}
    for lg in db["logos"]:
        cid = lg.get("channel")
        if not cid:
            continue
        if cid not in ch_logo or lg.get("in_use"):
            ch_logo[cid] = lg["url"]

    candidates = []
    for cid, c in channels.items():
        if cid in block or c.get("is_nsfw"):
            continue
        streams = ch_streams.get(cid)
        if not streams:
            continue
        langs = ch_langs.get(cid, set())
        cats = set(c.get("categories") or [])
        country = c.get("country")
        mfl = main_feed_langs.get(cid, [])
        primary = mfl[0] if mfl else None
        is_anime = (any(k in c["name"].lower() for k in ANIME_KW)
                    and (langs & ANIME_LANGS))

        if is_anime:
            bucket, group = "anime", "Anime"
        elif "hin" in langs:
            bucket, group = "hindi", primary_group("Hindi", cats)
        elif country == "IN" and "eng" in langs:
            bucket, group = "eng_in", primary_group("English (India)", cats)
        elif primary == "eng" and country != "IN" and (cats & FOREIGN_CATS):
            bucket = "eng_foreign"
            cat = next(c2 for c2 in ("movies", "sports", "news", "entertainment")
                       if c2 in cats)
            group = f"English (Intl) - {cat.capitalize()}"
        else:
            continue

        # rank streams: main feed first, then quality, then https
        mf = main_feed.get(cid)
        streams.sort(key=lambda s: (s.get("feed") == mf,
                                    quality_score(s.get("quality")),
                                    s["url"].startswith("https")),
                     reverse=True)
        candidates.append({
            "id": cid,
            "name": c["name"],
            "bucket": bucket,
            "group": group,
            "logo": ch_logo.get(cid, ""),
            "streams": streams,
        })
    return candidates


# ----------------------------------------------------------------------- validation
def probe(url: str, ua: str | None, ref: str | None, timeout: float) -> bool:
    headers = {"User-Agent": ua or DEFAULT_UA}
    if ref:
        headers["Referer"] = ref
    headers["Range"] = "bytes=0-2047"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            if code not in (200, 206):
                return False
            chunk = r.read(2048)
            return bool(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            ConnectionError, OSError, ValueError):
        return False


def pick_working_stream(chan: dict, timeout: float, max_try: int) -> dict | None:
    """Return the first reachable stream (early-stop over quality-ranked list)."""
    for s in chan["streams"][:max_try]:
        if probe(s["url"], s.get("user_agent"), s.get("referrer"), timeout):
            return s
    return None


# ---------------------------------------------------------------------------- output
BUCKET_ORDER = {"hindi": 0, "eng_in": 1, "eng_foreign": 2, "anime": 3}


def write_m3u(path: str, rows: list[dict], epg_url: str | None):
    header = "#EXTM3U"
    if epg_url:
        header += f' x-tvg-url="{epg_url}"'
    lines = [header]
    rows.sort(key=lambda r: (BUCKET_ORDER[r["bucket"]], r["group"], r["name"].lower()))
    for r in rows:
        s = r["stream"]
        attrs = (f'tvg-id="{r["id"]}" tvg-logo="{r["logo"]}" '
                 f'group-title="{r["group"]}"')
        lines.append(f'#EXTINF:-1 {attrs},{r["name"]}')
        if s.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={s["referrer"]}')
        if s.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={s["user_agent"]}')
        lines.append(s["url"])
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


# ------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="re-download source data from iptv-org")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip stream reachability probing (faster, keeps dead/geo links)")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--max-try", type=int, default=4,
                    help="max streams to probe per channel before giving up")
    ap.add_argument("--out", default=os.path.join(HERE, "playlist.m3u"))
    ap.add_argument("--epg-url", default="https://raw.githubusercontent.com/wizakorhd/iptv/main/guide.xml",
                    help="value for x-tvg-url in the playlist header")
    ap.add_argument("--ids-out", default=os.path.join(HERE, "curated_ids.json"),
                    help="write the final curated channel ids (for EPG generation)")
    args = ap.parse_args()

    print("Loading iptv-org data ...", file=sys.stderr)
    db = load(args.refresh)
    cands = build_candidates(db)
    print(f"Candidates after curation rules: {len(cands)}", file=sys.stderr)

    rows = []
    if args.no_validate:
        for c in cands:
            rows.append({**c, "stream": c["streams"][0]})
    else:
        print(f"Validating streams (workers={args.workers}, "
              f"timeout={args.timeout}s) ...", file=sys.stderr)
        done = 0
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(pick_working_stream, c, args.timeout, args.max_try): c
                    for c in cands}
            for fut in cf.as_completed(futs):
                done += 1
                if done % 100 == 0:
                    print(f"  probed {done}/{len(cands)} ...", file=sys.stderr)
                s = fut.result()
                if s:
                    c = futs[fut]
                    rows.append({**c, "stream": s})

    # summary
    by_bucket = defaultdict(int)
    for r in rows:
        by_bucket[r["bucket"]] += 1
    print("\n=== Curated playlist summary ===", file=sys.stderr)
    print(f"  Hindi ................. {by_bucket['hindi']}", file=sys.stderr)
    print(f"  English (India) ...... {by_bucket['eng_in']}", file=sys.stderr)
    print(f"  English (Intl) ....... {by_bucket['eng_foreign']}", file=sys.stderr)
    print(f"  Anime ................ {by_bucket['anime']}", file=sys.stderr)
    print(f"  TOTAL channels ....... {len(rows)}", file=sys.stderr)

    write_m3u(args.out, rows, args.epg_url)
    json.dump(sorted(r["id"] for r in rows), open(args.ids_out, "w"), indent=1)
    print(f"\nWrote {args.out}", file=sys.stderr)
    print(f"Wrote {args.ids_out} ({len(rows)} ids for EPG)", file=sys.stderr)


if __name__ == "__main__":
    main()
