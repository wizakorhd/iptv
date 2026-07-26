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
import http.client
import json
import os
import re
import xml.etree.ElementTree as ET
import sys
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter, defaultdict

API = "https://iptv-org.github.io/api"
FILES = ["channels", "streams", "feeds", "categories", "languages",
         "countries", "blocklist", "logos"]
FOREIGN_CATS = {"movies", "entertainment", "sports", "news", "documentary"}
# Anime is not a category in the dataset, so detect dedicated anime channels by name.
ANIME_KW = ("anime", "animax", "ani-one", "ani one", "aniplus", "toonami",
            "crunchyroll", "one piece", "naruto", "pokemon", "dragon ball",
            "gundam", "hidive", "filmrise anime", "kanade")
# We accept anime as sub or dub, so English- or Japanese-audio anime only
# (skips Spanish/Portuguese/German-only anime feeds).
ANIME_LANGS = {"eng", "jpn", "jap"}

# Themed sub-categories detected primarily by channel name (these aren't first-class
# iptv-org categories). Applied on top of the bucket to refine the group label, and
# they also let a foreign channel qualify even if its iptv-org category isn't in
# FOREIGN_CATS (e.g. a Reality/Horror channel tagged only "lifestyle"/"family").
# Keyword lists + exclusions are refined from a research pass (high precision).
# NOTE: matched at a token start (see theme_of), so "fear " needs its trailing space
# to avoid "Fearless"; bare risky tokens ("history", "id", "own", "wild", "turbo")
# are deliberately avoided in favour of specific phrases.
HORROR_KW = ("horror", "scream", "chiller", "screambox", "shudder",
             "dark matter tv", "darkmatter", "haunttv", "haunt tv",
             "fright", "terror", "macabre", "nightmare", "slasher", "thriller")
REALITY_KW = ("tlc", "bravo", "slice", "real time", "realtime", "we tv", "wetv",
              "own network", "hallmark", "lifetime", "reality", "real housewives",
              "housewives", "keeping up", "kardashian", "big brother", "hgtv",
              "love island", "bachelor", "e! ", "e! entertainment")
DOC_KW = ("discovery", "nat geo", "national geographic", "natgeo",
          "animal planet", "history tv", "history channel", "history hd",
          "the history channel", "investigation discovery", "science channel",
          "smithsonian", "curiosity", "curiositystream", "love nature",
          "bbc earth", "travelxp", "travel xp", "magellan", "real wild",
          "planet earth", "geographic", "pbs", "docubay", "epic drama",
          "quest", "yesterday", "blaze")
# skip theming when one of these appears (traps flagged by research)
THEME_EXCLUDE = ("real madrid", "history of", "wild fm", "turbo gaming", "downtown")
# iptv-org category slugs that reinforce the Documentary theme
DOC_CATS = {"documentary", "science", "travel", "outdoor"}


def _kw_re(words) -> "re.Pattern":
    # match a keyword only at a token start (non-alphanumeric or string start
    # before it), so "e!" doesn't match inside "Charge!" nor "we tv" in "Lolwe TV".
    return re.compile(r"(?<![a-z0-9])(?:" +
                      "|".join(re.escape(w.strip()) for w in words) + r")")


_HORROR_RE = None
_REALITY_RE = None
_DOC_RE = None


def theme_of(name: str, cats: set[str]) -> str | None:
    """Return a themed sub-category (Horror/Reality/Documentary) or None."""
    global _HORROR_RE, _REALITY_RE, _DOC_RE
    if _HORROR_RE is None:
        _HORROR_RE = _kw_re(HORROR_KW)
        _REALITY_RE = _kw_re(REALITY_KW)
        _DOC_RE = _kw_re(DOC_KW)
    n = name.lower()
    if any(x in n for x in THEME_EXCLUDE):
        return None
    if _HORROR_RE.search(n):
        return "Horror"
    if _REALITY_RE.search(n):
        return "Reality"
    if (cats & DOC_CATS) or _DOC_RE.search(n):
        return "Documentary"
    return None
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
        theme = theme_of(c["name"], cats)

        if is_anime:
            bucket, group = "anime", "Anime"
        elif "hin" in langs:
            bucket = "hindi"
            group = f"Hindi - {theme}" if theme else primary_group("Hindi", cats)
        elif country == "IN" and "eng" in langs:
            bucket = "eng_in"
            group = (f"English (India) - {theme}" if theme
                     else primary_group("English (India)", cats))
        elif "eng" in langs and country != "IN" and ((cats & FOREIGN_CATS) or theme):
            bucket = "eng_foreign"
            if theme:
                group = f"English (Intl) - {theme}"
            else:
                cat = next(c2 for c2 in ("movies", "sports", "news",
                                         "documentary", "entertainment")
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
def _get(url: str, headers: dict, timeout: float, maxbytes: int):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.geturl(), r.read(maxbytes)


def _probe_hls(url: str, headers: dict, timeout: float, depth: int = 0) -> bool:
    """Validate an HLS stream by resolving to a media playlist and pulling a real
    segment — a manifest returning 200 is not enough (segments can still be dead).
    Relative URIs are resolved against the *final* URL after redirects (important
    for redirector links like Pluto's jmp2.uk that point at another host)."""
    code, final_url, body = _get(url, headers, timeout, 16384)
    if code not in (200, 206) or not body:
        return False
    text = body.decode("utf-8", "replace")
    if "#EXTM3U" not in text:
        return False
    uris = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]
    if not uris:
        return False
    if "EXT-X-STREAM-INF" in text and depth < 2:      # master -> follow a variant
        return _probe_hls(urllib.parse.urljoin(final_url, uris[0]), headers,
                          timeout, depth + 1)
    # media playlist: fetch a segment away from the live edge (the newest segment
    # may not be flushed yet -> false 404s).
    seg = urllib.parse.urljoin(final_url, uris[len(uris) // 2])
    h = dict(headers)
    h["Range"] = "bytes=0-65535"
    try:
        code, _, chunk = _get(seg, h, timeout, 65536)
    except urllib.error.HTTPError:
        return False                                  # explicit 4xx/5xx -> dead
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return True                                   # transient -> keep (manifest was live)
    return code in (200, 206) and len(chunk) > 512


def probe(url: str, ua: str | None, ref: str | None, timeout: float) -> bool:
    headers = {"User-Agent": ua or DEFAULT_UA}
    if ref:
        headers["Referer"] = ref
    try:
        if ".m3u8" in url.lower():
            return _probe_hls(url, headers, timeout)
        h = dict(headers)
        h["Range"] = "bytes=0-2047"
        code, _, chunk = _get(url, h, timeout, 2048)
        return code in (200, 206) and bool(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            ConnectionError, OSError, ValueError, http.client.HTTPException):
        return False
    except Exception:
        # Probing untrusted third-party servers can raise almost anything
        # (malformed chunked encoding, decode errors, ...). Any failure just
        # means "not usable" — it must never abort the whole build.
        return False


def pick_working_stream(chan: dict, timeout: float, max_try: int) -> dict | None:
    """Return the first reachable stream (early-stop over quality-ranked list)."""
    for s in chan["streams"][:max_try]:
        if probe(s["url"], s.get("user_agent"), s.get("referrer"), timeout):
            return s
    return None


# ---------------------------------------------------------------------------- output
BUCKET_ORDER = {"hindi": 0, "eng_in": 1, "eng_foreign": 2, "anime": 3}
BUCKET_LABEL = {"hindi": "Hindi", "eng_in": "English (India)",
                "eng_foreign": "English (Intl)", "anime": "Anime"}
BUCKET_FILE = {"hindi": "playlist-hindi.m3u", "eng_in": "playlist-english-india.m3u",
               "eng_foreign": "playlist-english-intl.m3u", "anime": "playlist-anime.m3u"}

# Popular channels for a quick-access "Top" playlist (matched case-insensitively
# against the channel name; only working ones are included).
TOP_NAMES = [
    # India news
    "aaj tak", "ndtv 24x7", "ndtv india", "republic", "india today", "times now",
    "wion", "cnbc tv18", "dd news", "abp news", "india tv",
    # India entertainment / movies
    "colors", "sony entertainment", "star bharat", "star gold", "zee cinema",
    "zee tv", "sony max", "sony pix", "set max", "&pictures", "sony wah",
    # India GEC / kids / doc
    "dd national", "national geographic", "history tv18", "food food",
    # International news
    "bbc news", "cnn", "al jazeera", "sky news", "france 24", "dw ", "cnbc",
    "bloomberg", "cgtn", "euronews", "abc news", "cbs news", "nbc news",
    # Documentary
    "national geographic", "nat geo", "discovery", "animal planet",
    "history tv18", "smithsonian", "pbs", "love nature", "travelxp", "bbc earth",
    # Reality
    "tlc", "bravo", "slice", "lifetime", "hallmark", "we tv", "hgtv",
    # Horror
    "haunttv", "dark matter tv", "screambox", "shudder",
    # International entertainment / movies / sports
    "pluto tv movies", "pluto tv action", "red bull", "fox sports", "dazn",
    "sony ten", "star sports",
]


def quality_tag(q: str | None) -> str:
    score = quality_score(q)
    if score >= 6:            # 2160p
        return " 4K"
    if score >= 4:            # 1080p / 1440p
        return " ᶠᴴᴰ"
    if score >= 3:            # 720p
        return " ᴴᴰ"
    return ""


def finalize(rows: list[dict]) -> list[dict]:
    """Sort rows, assign a stable sequential channel number (tvg-chno), and bake a
    global per-group channel count into the group title (Lume/other players don't
    show per-category counts on their own, e.g. 'Hindi - News (23)')."""
    rows.sort(key=lambda r: (BUCKET_ORDER[r["bucket"]], r["group"], r["name"].lower()))
    group_counts = Counter(r["group"] for r in rows)
    for i, r in enumerate(rows, start=1):
        r["chno"] = i
        r["group_display"] = f'{r["group"]} ({group_counts[r["group"]]})'
        tag = quality_tag(r["stream"].get("quality"))
        # avoid doubling a tag if the name already ends with HD/4K
        base = r["name"]
        r["display"] = base if base.rstrip().upper().endswith(("HD", "4K")) else base + tag
    return rows


def write_m3u(path: str, rows: list[dict], epg_url: str | None):
    header = "#EXTM3U"
    if epg_url:
        header += f' x-tvg-url="{epg_url}"'
    lines = [header]
    for r in rows:
        s = r["stream"]
        group = r.get("group_display", r["group"])
        attrs = (f'tvg-id="{r["id"]}" tvg-chno="{r["chno"]}" '
                 f'tvg-logo="{r["logo"]}" group-title="{group}"')
        lines.append(f'#EXTINF:-1 {attrs},{r["display"]}')
        if s.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={s["referrer"]}')
        if s.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={s["user_agent"]}')
        lines.append(s["url"])
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def write_report(path: str, rows: list[dict], n_candidates: int, epg_ids: set):
    from datetime import datetime, timezone
    by_bucket = defaultdict(list)
    for r in rows:
        by_bucket[r["bucket"]].append(r)
    no_logo = sum(1 for r in rows if not r["logo"])
    no_epg = sum(1 for r in rows if r["id"] not in epg_ids)
    lines = [
        "# Playlist health report", "",
        f"_Last built: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_", "",
        f"- **Total channels:** {len(rows)}",
        f"- **Candidates before validation:** {n_candidates} "
        f"(dropped {n_candidates - len(rows)} dead/geo-blocked from build location)",
        f"- **Channels without EPG guide:** {no_epg}",
        f"- **Channels without a logo:** {no_logo}", "",
        "## By group", "", "| Group | Channels | File |", "|---|---:|---|",
    ]
    for b in sorted(by_bucket, key=lambda x: BUCKET_ORDER[x]):
        lines.append(f"| {BUCKET_LABEL[b]} | {len(by_bucket[b])} | `{BUCKET_FILE[b]}` |")
    lines += ["", "## Sub-groups", "", "| Sub-group | Channels |", "|---|---:|"]
    sub = defaultdict(int)
    for r in rows:
        sub[r["group"]] += 1
    for g in sorted(sub):
        lines.append(f"| {g} | {sub[g]} |")
    lines.append("")
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
    ap.add_argument("--epg-url", default="https://raw.githubusercontent.com/wizakorhd/iptv/main/guide.xml.gz",
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
                try:
                    s = fut.result()
                except Exception:
                    s = None                          # a worker crash never aborts the run
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

    rows = finalize(rows)
    write_m3u(args.out, rows, args.epg_url)
    json.dump(sorted(r["id"] for r in rows), open(args.ids_out, "w"), indent=1)

    # per-category playlists
    for b, fname in BUCKET_FILE.items():
        subset = [r for r in rows if r["bucket"] == b]
        if subset:
            write_m3u(os.path.join(os.path.dirname(args.out) or ".", fname),
                      subset, args.epg_url)

    # "Top" quick-access playlist of popular working channels
    top = [r for r in rows
           if any(k in r["name"].lower() for k in TOP_NAMES)]
    if top:
        write_m3u(os.path.join(os.path.dirname(args.out) or ".", "playlist-top.m3u"),
                  top, args.epg_url)
        print(f"  Top (popular) ........ {len(top)}", file=sys.stderr)

    # health report: prefer the actual guide (reflects the epgshare merge too);
    # fall back to guides.json (iptv-org sources) when the guide isn't built yet.
    epg_ids = set()
    base_dir = os.path.dirname(args.out) or "."
    guide_xml = os.path.join(base_dir, "guide.xml")
    guide_gz = os.path.join(base_dir, "guide.xml.gz")
    guide_fh = None
    if os.path.exists(guide_xml):
        guide_fh = open(guide_xml, "rb")
    elif os.path.exists(guide_gz):
        import gzip
        guide_fh = gzip.open(guide_gz, "rb")
    if guide_fh is not None:
        try:
            with guide_fh:
                for _, el in ET.iterparse(guide_fh, events=("end",)):
                    if el.tag == "channel":
                        epg_ids.add(el.get("id"))
                        el.clear()
        except Exception:
            epg_ids = set()
    if not epg_ids:
        gpath = os.path.join(DATA, "guides.json")
        if os.path.exists(gpath):
            try:
                for g in json.load(open(gpath)):
                    epg_ids.add(g["channel"])
            except Exception:
                pass
    write_report(os.path.join(os.path.dirname(args.out) or ".", "REPORT.md"),
                 rows, len(cands), epg_ids)

    print(f"\nWrote {args.out}", file=sys.stderr)
    print(f"Wrote per-category playlists + REPORT.md", file=sys.stderr)
    print(f"Wrote {args.ids_out} ({len(rows)} ids for EPG)", file=sys.stderr)


if __name__ == "__main__":
    main()
