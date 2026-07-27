#!/usr/bin/env python3
"""
Curated IPTV playlist generator (built on the free, community iptv-org dataset).

Curation rules
--------------
  * Hindi channels ......... all categories (language contains 'hin')
  * English Indian ......... all categories (country == IN and language contains 'eng')
  * English foreign ........ categories in {movies, entertainment, sports, news}
                             (language contains 'eng', country != IN)
  * FAST channels .......... Samsung TV Plus (India + intl) not indexed by
                             iptv-org, listed via i.mjh.nz and resolved through
                             jmp2.uk; curated with the same rules above.

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
import time
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter, defaultdict

API = "https://iptv-org.github.io/api"
FILES = ["channels", "streams", "feeds", "categories", "languages",
         "countries", "blocklist", "logos"]
FOREIGN_CATS = {"movies", "entertainment", "sports", "news", "documentary",
                "kids", "music", "comedy", "animation", "family", "lifestyle",
                "culture", "cooking", "science", "travel", "education",
                "series", "classic", "auto", "outdoor", "weather", "relax",
                "business", "legislative"}
# Categories we never carry regardless of language/region: devotional content
# (per curation), adult, teleshopping, and uncategorised "general" bloat.
EXCLUDE_CATS = {"religious", "xxx", "shop"}
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


# --------------------------------------------------------------------------- FAST
# Free ad-supported streaming (FAST) channels that iptv-org doesn't fully index
# (it only carries channels that have an iptv-org *channel record*; many Samsung
# TV Plus FAST channels have none). We take the channel list from matthuisman's
# i.mjh.nz (already our EPG provider) and resolve each stream through his jmp2.uk
# redirector -> the platform CDN (Samsung TV Plus is Amagi-backed, the same infra
# as our existing entries). The Samsung channel id doubles as the tvg-id, so the
# guide comes straight from the matching i.mjh.nz XMLTV (see merge_fast_epg.py).
SAMSUNG_CHANNELS = "https://i.mjh.nz/SamsungTVPlus/.channels.json.gz"
SAMSUNG_STREAM = "https://jmp2.uk/stvp-{id}"
# India -> curated as Hindi / English-India (all categories); the rest -> English
# foreign (kept only for on-target categories, see below).
SAMSUNG_REGIONS = ("in", "us", "gb", "ca")

# Indian regional languages we exclude (curation = Hindi + English-Indian only).
# Korean is intentionally kept (subbed) per user request, grouped separately.
# Includes regional-network brand names that don't carry the language in the name
# (ETV=Telugu, Asianet=Malayalam, Jomjomat=Bengali, Sun/Gemini/Udaya/Surya, ...).
REGIONAL_EXCLUDE_KW = ("tamil", "telugu", "kannada", "malayalam", "bengali",
                       "bangla", "marathi", "gujarati", "punjabi", "bhojpuri",
                       "odia", "oriya", "assamese", "nepali", "urdu",
                       "etv", "asianet", "suvarna", "jomjomat", "gemini",
                       "udaya", "surya tv", "kairali", "raj tv", "kalaignar",
                       "polimer", "vijay tv", "sun tv", "south station",
                       "south flix", "zee south", "aha ")
KOREAN_KW = ("korean", "k-pop", "kpop", "kocowa", "k by mbc", " mbc", " kbs")

# Samsung group label -> our category slug.
SAMSUNG_CAT = {
    "movies": "movies", "movie": "movies",
    "news": "news", "news & opinion": "news", "business": "business",
    "english news": "news", "hindi news": "news",
    "sports": "sports", "sport": "sports", "sports & outdoors": "sports",
    "motor sports": "sports",
    "documentaries": "documentary", "nature, history & science": "documentary",
    "nature": "documentary", "infotainment": "documentary",
    "entertainment": "entertainment", "action & drama": "entertainment",
    "tv series": "series", "comedy": "comedy", "drama": "entertainment",
    "crime": "entertainment", "sci-fi & horror": "entertainment",
    "reality": "entertainment", "reality tv": "entertainment",
    "reality competition": "entertainment", "western & classic tv": "classic",
    "game shows": "entertainment", "anime & gaming": "entertainment",
    "music": "music", "music & ambient": "music", "ambiance": "relax",
    "kids": "kids",
    "home & food": "cooking", "food & travel": "cooking",
    "lifestyle": "lifestyle", "lifestyle & pop culture": "lifestyle",
}
# Samsung groups we never carry (devotional per curation, Latino = Spanish).
SAMSUNG_EXCLUDE_GROUPS = {"devotional", "latino"}


def norm_name(s: str) -> str:
    """Normalized display name for dedup. Drops quality/format words and the
    language/region qualifiers Samsung appends (e.g. "History TV18 - Hindi" and
    "History TV18" must collide) so FAST channels don't duplicate iptv-org ones."""
    s = s.lower()
    s = re.sub(r"\b(hd|fhd|uhd|4k|sd|tv|channel|the|hindi|english|india|indian)\b",
               " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def fast_candidates(existing_names: set[str], refresh: bool = False) -> list[dict]:
    """Samsung TV Plus channels (IN + intl) not already covered by iptv-org,
    curated with the same rules and shaped like build_candidates() rows."""
    import gzip
    cache = os.path.join(DATA, "samsung.channels.json.gz")
    try:
        if refresh and os.path.exists(cache):
            os.remove(cache)
        if not os.path.exists(cache):
            os.makedirs(DATA, exist_ok=True)
            req = urllib.request.Request(SAMSUNG_CHANNELS,
                                         headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                open(cache, "wb").write(r.read())
        with gzip.open(cache, "rb") as fh:
            data = json.load(fh)
    except Exception as e:
        print(f"  ! Samsung TV Plus source unavailable: {e}", file=sys.stderr)
        return []

    regions = data.get("regions", {})
    seen = set(existing_names)
    out = []
    for reg in SAMSUNG_REGIONS:
        for cid, c in regions.get(reg, {}).get("channels", {}).items():
            name = (c.get("name") or "").strip()
            nn = norm_name(name)
            if not name or not nn or nn in seen:
                continue
            low = name.lower()
            grp_raw = (c.get("group") or "").strip().lower()
            cat = SAMSUNG_CAT.get(grp_raw)
            theme = theme_of(name, {cat} if cat else set())

            if any(k in low for k in ANIME_KW):
                bucket, group = "anime", "Anime"
            elif any(k in low for k in KOREAN_KW):
                bucket, group = "eng_foreign", "Korean"
            elif reg == "in":
                if (grp_raw in SAMSUNG_EXCLUDE_GROUPS or "regional" in grp_raw
                        or any(k in low for k in REGIONAL_EXCLUDE_KW)):
                    continue
                bucket = "hindi"
                group = f"Hindi - {theme or (cat.capitalize() if cat else 'General')}"
            else:  # intl -> English foreign, on-target categories only
                if grp_raw in SAMSUNG_EXCLUDE_GROUPS:
                    continue
                if not (theme or (cat in FOREIGN_CATS)):
                    continue
                bucket = "eng_foreign"
                group = f"English (Intl) - {theme or cat.capitalize()}"

            seen.add(nn)
            out.append({
                "id": cid,
                "name": name,
                "bucket": bucket,
                "group": group,
                "logo": c.get("logo", ""),
                "streams": [{"url": SAMSUNG_STREAM.format(id=cid),
                             "quality": None, "feed": None}],
            })
    return out


# The Roku Channel (US FAST). Same matthuisman backend + jmp2 redirector, but the
# metadata carries no category, so we label by name (guess_cat/theme) and only
# drop devotional-by-name entries. Streams resolve to Roku's own CDN (no token).
ROKU_CHANNELS = "https://i.mjh.nz/Roku/.channels.json.gz"
ROKU_STREAM = "https://jmp2.uk/rok-{id}.m3u8"
ROKU_DEVOTIONAL_KW = ("faith", "church", "gospel", "daystar", "ewtn", "bible",
                      "word network", "gaither", "trinity broadcast", " tbn",
                      "prayer", "ministries", "catholic", "christian", "hillsong")
# Name-keyword -> category label (Roku has no category field).
NAME_CAT = (
    (("news", "cnn", "fox news", "abc news", "cbs news", "nbc news", "msnbc",
      "bloomberg", "weather", "newsmax", "cheddar"), "news"),
    (("movie", "cinema", "films", "flix", "grindhouse"), "movies"),
    (("sport", "nfl", "nba", "mlb", "nhl", "soccer", "golf", "poker", "wwe",
      "fight", "racing", "outdoor"), "sports"),
    (("kids", "cartoon", "toon", "baby", "kid ", "pbs kids", "nick"), "kids"),
    (("music", "mtv", "vevo", "radio", "beats", "hits", "classical"), "music"),
    (("comedy", "funny", "laugh"), "comedy"),
    (("food", "cook", "kitchen", "recipe"), "cooking"),
    (("travel", "explore"), "travel"),
)


def guess_cat(name: str) -> str | None:
    low = name.lower()
    for kws, cat in NAME_CAT:
        if any(k in low for k in kws):
            return cat
    return None


def roku_candidates(existing_names: set[str], refresh: bool = False) -> list[dict]:
    """The Roku Channel (US) FAST channels not already covered, shaped like rows."""
    import gzip
    cache = os.path.join(DATA, "roku.channels.json.gz")
    try:
        if refresh and os.path.exists(cache):
            os.remove(cache)
        if not os.path.exists(cache):
            os.makedirs(DATA, exist_ok=True)
            req = urllib.request.Request(ROKU_CHANNELS,
                                         headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                open(cache, "wb").write(r.read())
        with gzip.open(cache, "rb") as fh:
            data = json.load(fh)
    except Exception as e:
        print(f"  ! Roku source unavailable: {e}", file=sys.stderr)
        return []

    seen = set(existing_names)
    out = []
    for cid, c in data.get("channels", {}).items():
        name = (c.get("name") or "").strip()
        nn = norm_name(name)
        if not name or not nn or nn in seen:
            continue
        low = name.lower()
        if any(k in low for k in ROKU_DEVOTIONAL_KW):
            continue
        if any(k in low for k in ANIME_KW):
            bucket, group = "anime", "Anime"
        elif any(k in low for k in KOREAN_KW):
            bucket, group = "eng_foreign", "Korean"
        else:
            bucket = "eng_foreign"
            cat = guess_cat(name)
            group = f"English (Intl) - {theme_of(name, set()) or (cat.capitalize() if cat else 'General')}"
        seen.add(nn)
        out.append({
            "id": cid,
            "name": name,
            "bucket": bucket,
            "group": group,
            "logo": c.get("logo", ""),
            "streams": [{"url": ROKU_STREAM.format(id=cid),
                         "quality": None, "feed": None}],
        })
    return out


# Pluto TV (US + GB FAST). Streams via jmp2.uk/plu-{id}.m3u8 -> Pluto's stitcher.
# Pluto serves a frozen promo-slate loop for content it can't licence in the
# request region, which passes ordinary liveness; drop_pluto_slates() removes
# those after validation (see below).
PLUTO_CHANNELS = "https://i.mjh.nz/PlutoTV/.channels.json.gz"
PLUTO_STREAM = "https://jmp2.uk/plu-{id}.m3u8"
PLUTO_REGIONS = ("us", "gb")
PLUTO_EXCLUDE_GROUPS = {"en español", "en espanol", "kids en français",
                        "kids en francais"}
PLUTO_CAT = {
    "reality": "entertainment", "competition reality": "entertainment",
    "big brother live": "entertainment", "drama": "entertainment",
    "bingeable drama": "entertainment", "crime drama": "entertainment",
    "true crime": "entertainment", "paranormal": "entertainment",
    "sci-fi": "entertainment", "sci-fi & fantasy": "entertainment",
    "sci-fi + fantasy": "entertainment", "entertainment": "entertainment",
    "daytime + game shows": "entertainment", "daytime & talk shows": "entertainment",
    "game shows": "entertainment", "new on pluto tv": "entertainment",
    "anime": "entertainment", "south park": "comedy",
    "movies": "movies", "christmas in july": "movies",
    "sports": "sports",
    "comedy": "comedy", "classic tv comedy": "comedy",
    "kids": "kids",
    "classic tv": "classic", "westerns": "classic",
    "local news": "news", "news + opinion": "news", "news": "news",
    "home + food": "cooking",
    "music videos": "music", "music": "music",
    "real life adventure": "outdoor", "animals + nature": "documentary",
    "documentaries": "documentary", "history + science": "documentary",
    "documentary + science": "documentary",
    "living": "lifestyle",
}


def pluto_candidates(existing_names: set[str], refresh: bool = False) -> list[dict]:
    """Pluto TV (US+GB) FAST channels not already covered, shaped like rows.
    Slate channels are pruned later by drop_pluto_slates()."""
    import gzip
    cache = os.path.join(DATA, "pluto.channels.json.gz")
    try:
        if refresh and os.path.exists(cache):
            os.remove(cache)
        if not os.path.exists(cache):
            os.makedirs(DATA, exist_ok=True)
            req = urllib.request.Request(PLUTO_CHANNELS,
                                         headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                open(cache, "wb").write(r.read())
        with gzip.open(cache, "rb") as fh:
            data = json.load(fh)
    except Exception as e:
        print(f"  ! Pluto TV source unavailable: {e}", file=sys.stderr)
        return []

    regions = data.get("regions", {})
    seen = set(existing_names)
    out = []
    for reg in PLUTO_REGIONS:
        for cid, c in regions.get(reg, {}).get("channels", {}).items():
            name = (c.get("name") or "").strip()
            nn = norm_name(name)
            if not name or not nn or nn in seen:
                continue
            grp_raw = (c.get("group") or "").strip().lower()
            if grp_raw in PLUTO_EXCLUDE_GROUPS:
                continue
            low = name.lower()
            cat = PLUTO_CAT.get(grp_raw)
            theme = theme_of(name, {cat} if cat else set())
            if any(k in low for k in ANIME_KW):
                bucket, group = "anime", "Anime"
            elif any(k in low for k in KOREAN_KW):
                bucket, group = "eng_foreign", "Korean"
            else:
                if not (theme or (cat in FOREIGN_CATS)):
                    continue
                bucket = "eng_foreign"
                group = f"English (Intl) - {theme or cat.capitalize()}"
            seen.add(nn)
            out.append({
                "id": cid,
                "name": name,
                "bucket": bucket,
                "group": group,
                "logo": c.get("logo", ""),
                "streams": [{"url": PLUTO_STREAM.format(id=cid),
                             "quality": None, "feed": None}],
            })
    return out


def _stream_content_hash(url: str, timeout: float) -> str | None:
    """md5 of the first media segment's leading bytes (master->media->segment).
    Used to detect Pluto slate loops: a live stream advances so the hash changes
    between two samples; a frozen slate returns the same hash."""
    import hashlib
    try:
        master, base = _fetch(url, timeout)
        media = next((l for l in master.decode("utf-8", "ignore").splitlines()
                      if l and not l.startswith("#")), None)
        if not media:
            return None
        mabs = urllib.parse.urljoin(base, media)
        mp, mbase = _fetch(mabs, timeout)
        seg = next((l for l in mp.decode("utf-8", "ignore").splitlines()
                    if l and not l.startswith("#")), None)
        if not seg:
            return None
        data, _ = _fetch(urllib.parse.urljoin(mbase, seg), timeout, cap=196608)
        return hashlib.md5(data).hexdigest() if data else None
    except Exception:
        return None


def _fetch(url: str, timeout: float, cap: int | None = None) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (r.read(cap) if cap else r.read()), r.geturl()


def drop_pluto_slates(rows: list[dict], workers: int, timeout: float,
                      delay: float = 30.0) -> list[dict]:
    """Remove Pluto channels stuck on a frozen slate/promo loop. A real live
    stream advances between two samples ~`delay`s apart (content hash changes);
    a slate stays byte-identical, so we drop channels whose hash doesn't move."""
    pluto = [r for r in rows if "jmp2.uk/plu-" in r["stream"]["url"]]
    if not pluto:
        return rows
    urls = [r["stream"]["url"] for r in pluto]

    def sample() -> dict:
        res = {}
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for u, h in zip(urls, ex.map(
                    lambda x: _stream_content_hash(x, timeout), urls)):
                res[u] = h
        return res

    print(f"Pluto slate check: sampling {len(urls)} channels "
          f"(2 passes, {delay:.0f}s apart) ...", file=sys.stderr)
    h1 = sample()
    time.sleep(delay)
    h2 = sample()
    slate = {u for u in urls
             if h1.get(u) and h2.get(u) and h1[u] == h2[u]}
    kept = [r for r in rows
            if "jmp2.uk/plu-" not in r["stream"]["url"]
            or r["stream"]["url"] not in slate]
    print(f"  dropped {len(slate)}/{len(pluto)} Pluto slate/frozen channels",
          file=sys.stderr)
    return kept


# --------------------------------------------------------------------- apsattv
# apsattv.com publishes flat M3U playlists of official FAST-service stream URLs
# (LG / TCL / Vidaa / Xiaomi / DistroTV / Tubi / Vizio / XUMO / Samsung regions ...)
# that iptv-org and the i.mjh.nz feeds don't fully index. They carry almost no
# category metadata (mostly group-title="Undefined") and frequently need a browser
# User-Agent, so we classify by name, keep only India + English channels (curation),
# drop devotional/adult/shopping/US-local, keep only recognisable genres (single-show
# "Uncategorized" noise is dropped, but acclaimed shows are rescued via the genre
# rules below), and let the India geo-validator prune anything that won't play.
APSAT_PAGE = "https://www.apsattv.com/streams.html"
APSAT_BASE = "https://www.apsattv.com/"
# Confirmed dead/blocked from India in a geo-viability sample, or fully redundant
# with feeds we already ingest via i.mjh.nz (rok/roku_all == our Roku adapter).
APSAT_DEAD = {"veely", "klowd", "rok", "roku_all", "rewardedtv", "ssungire",
              "zeasn", "freemoviesplus", "ssungth", "freetv"}
# Non-English-language markets: we only rescue India-language channels from these;
# their local content is out of scope (curation = English + Indian languages only).
APSAT_NONEN_SRC = {"ssungbra", "ssungbelg", "ssungden", "ssungfin", "ssungnor",
                   "ssungpor", "ssungswe", "ssungmex", "ssungneth", "ssunglux",
                   "ssungph", "rakuten-jp", "rakutentv-fr", "moviearkbr",
                   "olhosnatv", "redeitv", "soultv", "brlg", "delg", "dklg",
                   "eslg", "filg", "frlg", "itlg", "jplg", "krlg", "nllg", "nolg",
                   "pllg", "ptlg", "selg", "pelg", "cllg", "arlg", "mxlg", "lulg",
                   "belg", "chlg", "atlg", "tclbr"}
APSAT_IND_RE = re.compile(
    r"\b(india|hindi|bharat|desi|bollywood|zee (?!mundo|nung|one)|zee|colors|"
    r"sony ?(?:sab|max|pal|wah|aath)|aaj tak|ndtv|republic|wion|dd |doordarshan|"
    r"gujarat|tamil|telugu|telegu|punjab|marathi|bengali|bangla|kannada|malayalam|"
    r"shemaroo|times now|abp|news ?18|tv9|sansad|goldmines|manoranjan|ptc|ghaint|"
    r"ishara)\b", re.I)
# Indian channels that broadcast in English -> English-India bucket.
APSAT_IND_EN_RE = re.compile(
    r"\b(india today|wion|republic (?:tv|world|bharat)?|cnn news ?18|"
    r"ndtv (?:profit|24|prime|world)|times now|mirror now|et now|news ?18 india)\b",
    re.I)
# Zee variants for non-Indian markets that the broad "zee" match would wrongly grab.
APSAT_IND_FALSE = re.compile(r"\bzee (mundo|nung|world|bolly ?movies? \(|one)", re.I)
# Devotional / adult / shopping / US-local -> always dropped.
APSAT_DROP_RE = re.compile(
    r"\b(gospel|church|worship|jesus|christ\b|bible|god ?tv|daystar|ewtn|hillsong|"
    r"faith tv|quran|bhajan|bhakti|devotion|gurbani|mandir|temple tv|aastha|"
    r"sanskar|catholic|hillsong|shalom|"
    r"xxx|porn|erotic|playboy|brazzers|adult\b|"
    r"teleshop|shop ?lc|homeshop|naaptol|qvc|hsn\b|jewelry tv|"
    r"localnow|igocast|by wisn|by wthr|kwyb|ktvb|madison wi|milwaukee)\b", re.I)
# US-local TV-station feeds (LocalNow platform + callsign-named stations) -> dropped.
APSAT_LOCAL_HOSTS = ("localnow", "amdvids.com", "fuelmedia.io")
APSAT_CALLSIGN_RE = re.compile(r"^[WK][A-Z]{2,4}\d?\b")      # WCPO, KULR, WSMV4 (case-sensitive)
APSAT_LOCAL_RE = re.compile(
    r"\b(?:abc|cbs|nbc|fox|pbs|cw|univision|telemundo|noticias) ?\d{1,2}\b"
    r"|\bnews ?(?:channel )?\d{1,2}\b|\beyewitness news\b"
    r"|\b(?:nashville|connecticut|san diego|sacramento|cincinnati|minneapolis|"
    r"seattle|tulsa|billings|yakima|\bbend\b|idaho|palm springs|los angeles|"
    r"washington d\.?c|st\.? paul|rochester|cleveland|columbus|indianapolis|"
    r"milwaukee|st\.? louis|kansas city|san antonio|oklahoma|albuquerque) news\b",
    re.I)


def _apsat_islocal(name: str, url: str) -> bool:
    if any(h in url for h in APSAT_LOCAL_HOSTS):
        return True
    return bool(APSAT_CALLSIGN_RE.search(name) or APSAT_LOCAL_RE.search(name))


# Broken/placeholder channel labels seen in the raw feeds (e.g. LG's "c1 to id").
APSAT_JUNK_RE = re.compile(r"\bc\d+ to id\b|^\W*$|^untitled", re.I)

# Ordered genre rules (first match wins). Acclaimed single-show channels are baked
# in so they survive the "drop Uncategorized" cut. cat slug -> group word.
_APSAT_GENRE_SRC = [
    ("news", "News",
     r"\b(news|noticias|cnn|bbc news|fox news|msnbc|cnbc|bloomberg|al jazeera|"
     r"euronews|sky news|newsmax|wion|ndtv|aaj tak|abp|republic|times now|weather|"
     r"africanews|france 24|dw news|dateline|opinion)\b"),
    ("sports", "Sports",
     r"\b(sports?|espn|nfl|nba|nhl|mlb|soccer|football|cricket|golf|tennis|ufc|wwe|"
     r"racing|fifa|rugby|boxing|motogp|\bf1\b|olympic|surf league|poker|darts|"
     r"wrestling|dazn|motorvision|strongman)\b"),
    ("horror", "Horror",
     r"\b(horror|scream|chiller|screambox|shudder|dark matter|haunt|fright|terror|"
     r"macabre|nightmare|slasher|paranormal|ghost hunters|alter)\b"),
    ("scifi", "Sci-Fi",
     r"\b(sci-?fi|science fiction|star trek|star wars|doctor who|stargate|"
     r"battlestar|alien nation|outer limits|twilight zone|\bdust\b|cyberpunk)\b"),
    ("kids", "Kids",
     r"\b(kids|cartoon|nick|disney|junior|toon|baby|pokemon|pbs kids|boomerang|"
     r"mr bean|dino)\b"),
    ("movies", "Movies",
     r"\b(movies?|cine|cinema|films?|hollywood|bollywood|filmrise|grindhouse|flix|"
     r"flixfling|moviesphere)\b"),
    ("music", "Music",
     r"\b(music|mtv|vevo|\bhits\b|classical|jazz|k-pop|country music|rock\b|"
     r"hip ?hop|dance)\b"),
    ("documentary", "Documentary",
     r"\b(documentary|docu|nature|wildlife|history|science|space|planet|discovery|"
     r"nat geo|geographic|smithsonian|curiosity|magellan|docsville)\b"),
    ("comedy", "Comedy",
     r"\b(comedy|sitcom|laugh|funny|seinfeld|frasier|cheers|blackadder|red dwarf)\b"),
    ("crime", "Crime",
     r"\b(crime|forensic|investigation|murder|true crime|cops\b|court tv|law ?&)\b"),
    ("reality", "Reality",
     r"\b(reality|tlc|bravo|slice|masterchef|hell.?s kitchen|top gear|"
     r"kitchen nightmares|housewives|kardashian|big brother|hgtv|love island|"
     r"bachelor|american ninja)\b"),
    ("cooking", "Food & Travel",
     r"\b(food|cook|recipe|kitchen\b|travel|lifestyle|home ?&|garden|fashion|diy|"
     r"craft|bob ross|tastemade|pets?)\b"),
    ("entertainment", "Entertainment",
     r"\b(entertain|drama|series|classic tv|xena|sherlock|walking dead|quantum leap|"
     r"comet|western|hercules|highlander|game show|variety|telenovela)\b"),
]
_APSAT_GENRE = None


def _apsat_genre(name: str):
    global _APSAT_GENRE
    if _APSAT_GENRE is None:
        _APSAT_GENRE = [(slug, word, re.compile(pat, re.I))
                        for slug, word, pat in _APSAT_GENRE_SRC]
    for slug, word, rx in _APSAT_GENRE:
        if rx.search(name):
            return slug, word
    return None, None


def _apsat_clean(name: str) -> str:
    """Strip channel-number prefixes, provider tags and geo/format suffixes so the
    display name and dedup key are clean (e.g. '277 bollywood-masala-tv' -> 'Bollywood Masala',
    'Hells Kitchen US (Australia)' -> 'Hells Kitchen US')."""
    n = re.sub(r"^\s*\d{1,4}\s+", "", name)               # leading channel number
    n = re.sub(r"\s*-?\s*tcl\b", "", n, flags=re.I)       # "-TCL" provider tag
    n = re.sub(r"\s*\((?:geo|asia|hi|us|usa|uk|gb|in|au|ca|fr|it|de|dk|co|fi|cl|mx|"
               r"eu|pa|sp|pt|nz|ar|se|no|nl|be|at|ch|lu|north america|australia)\)",
               "", n, flags=re.I)
    n = re.sub(r"\bgeo\b|\bnot listed\b|\bwrong c\b|\balt\b", "", n, flags=re.I)
    n = n.replace("-", " ") if n.count("-") >= 2 else n   # "bollywood-masala-tv"
    return re.sub(r"\s{2,}", " ", n).strip(" -")


def apsattv_candidates(existing_names: set[str], refresh: bool = False) -> list[dict]:
    """Ingest apsattv.com's FAST playlists, curated to India + English channels in
    recognised genres, with per-channel User-Agent preserved (many CDNs 403 without
    one). Category labels are provisional (name-based); the geo-validator prunes
    channels that don't play from India."""
    cdir = os.path.join(DATA, "apsattv")
    os.makedirs(cdir, exist_ok=True)
    # discover the list of playlists from the streams page (resilient to changes)
    try:
        lpath = os.path.join(cdir, "_lists.txt")
        if refresh or not os.path.exists(lpath):
            req = urllib.request.Request(APSAT_PAGE, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                html = r.read().decode("utf-8", "ignore")
            names = sorted(set(re.findall(r"([A-Za-z0-9_.-]+\.m3u)", html)))
            open(lpath, "w").write("\n".join(names))
        lists = [x for x in open(lpath).read().splitlines() if x]
    except Exception as e:
        print(f"  ! apsattv source unavailable: {e}", file=sys.stderr)
        return []

    extinf = re.compile(r"#EXTINF:.*?,(.*)$")
    attr = lambda k, l: (re.search(k + r'="([^"]*)"', l) or [None, ""])[1]
    seen = set(existing_names)
    seen_url = set()
    bynn = {}
    out = []
    for fname in lists:
        src = fname[:-4]
        if src in APSAT_DEAD:
            continue
        path = os.path.join(cdir, fname)
        try:
            if refresh or not os.path.exists(path):
                req = urllib.request.Request(APSAT_BASE + fname,
                                             headers={"User-Agent": DEFAULT_UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    open(path, "wb").write(r.read())
            lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        except Exception:
            continue
        i = 0
        while i < len(lines):
            if not lines[i].startswith("#EXTINF"):
                i += 1
                continue
            ext = lines[i]
            raw = (extinf.search(ext) or [None, ""])[1].strip()
            if raw.startswith("tvg") or '="' in raw:      # malformed EXTINF attrs
                raw = raw.split(",")[-1].strip()
            logo = attr("tvg-logo", ext)
            ua = attr("http-user-agent", ext)
            url = ""
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j] and not lines[j].startswith("#"):
                    url = lines[j].strip()
                    i = j
                    break
            i += 1
            if not raw or not url or url in seen_url:
                continue
            name = _apsat_clean(raw)
            if not name or APSAT_JUNK_RE.search(name) or APSAT_DROP_RE.search(name):
                continue
            is_india = bool(APSAT_IND_RE.search(name)) and not APSAT_IND_FALSE.search(name)
            slug, word = _apsat_genre(name)
            if is_india and src not in APSAT_NONEN_SRC:
                bucket = "eng_in" if APSAT_IND_EN_RE.search(name) else "hindi"
                group = f"India - {word or 'General'}"
            else:
                # English foreign: english-market list + ascii name + known genre,
                # excluding US-local station feeds.
                if src in APSAT_NONEN_SRC or _apsat_islocal(name, url):
                    continue
                letters = [c for c in name if c.isalpha()]
                if not letters or sum(c.isascii() for c in letters) / len(letters) < 0.85:
                    continue
                if not slug:                              # Uncategorized noise -> skip
                    continue
                bucket = "anime" if slug == "kids" and any(
                    k in name.lower() for k in ANIME_KW) else "eng_foreign"
                group = "Anime" if bucket == "anime" else f"English (Intl) - {word}"
            nn = norm_name(name)
            if not nn or nn in seen:
                # backfill a logo onto an already-kept apsattv channel that lacked
                # one (LG/Xiaomi feeds ship no logo; the same channel on DistroTV
                # etc. does), so duplicates across lists still contribute a logo.
                prev = bynn.get(nn)
                if prev is not None and logo and not prev["logo"]:
                    prev["logo"] = logo
                continue
            seen.add(nn)
            seen_url.add(url)
            cand = {
                "id": attr("tvg-id", ext) or f"apsat-{src}-{nn}",
                "name": name,
                "bucket": bucket,
                "group": group,
                "logo": logo,
                "streams": [{"url": url, "quality": None, "feed": None,
                             "user_agent": ua or DEFAULT_UA}],
            }
            bynn[nn] = cand
            out.append(cand)
    return out


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
    for c in ("movies", "sports", "news", "kids", "music", "comedy",
              "documentary", "science", "travel", "cooking", "animation",
              "family", "lifestyle", "culture", "education", "classic",
              "auto", "outdoor", "business", "series", "entertainment"):
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
        # devotional/adult/teleshopping are never carried (per curation).
        if cats & EXCLUDE_CATS:
            continue
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
            group = (f"English (Intl) - {theme}" if theme
                     else primary_group("English (Intl)", cats))
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
        h["Range"] = "bytes=0-4095"
        code, _, chunk = _get(url, h, timeout, 4096)
        if code not in (200, 206) or not chunk:
            return False
        # Extension-less URLs (e.g. jmp2.uk FAST redirectors) can still be HLS —
        # deep-validate when the body sniffs as a manifest.
        if b"#EXTM3U" in chunk[:256]:
            return _probe_hls(url, headers, timeout)
        return True
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


# Region prefixes are consolidated to three (+ top-level Anime); the granular
# genre tail is merged into a canonical set so we don't end up with 50+ tiny
# groups. Keeps the genres the user explicitly cares about (Horror/Sci-Fi/Crime).
GENRE_CANON = {
    "business": "News",
    "classic": "Movies",
    "auto": "Entertainment",
    "education": "Documentary",
    "culture": "Documentary",
    "relax": "Music",
    "animation": "Kids",
    "family": "Kids",
    "cooking": "Food & Travel",
    "travel": "Food & Travel",
    "lifestyle": "Food & Travel",
}
REGION_CANON = {"india": "English (India)"}

# Indian regional-language filter: keep only Hindi + English. Matches distinct
# regional languages (South Indian, Bengali, Marathi, Gujarati, Punjabi, Odia,
# Assamese/NE, Urdu, Kashmiri) by language token or known regional brand.
# Deliberately does NOT match Hindi-belt state names (Bihar/UP/MP/Rajasthan/
# Haryana/Himachal/Uttarakhand/Jharkhand/Chhattisgarh/Delhi/J&K) which broadcast
# in Hindi, nor Hindi-belt dialect state channels.
REGIONAL_LANG_RE = re.compile(
    r"\b(tamil|telugu|telegu|kannada|malayalam|kerala|bangla|bengali|marathi|"
    r"gujarati|punjabi|odia|oriya|assam(?:ese)?|kashir|manipur|mizoram|"
    r"nagaland|meghalaya|konkani|tulu|sindhi|haryanvi)\b", re.I)
REGIONAL_BRAND_RE = re.compile(
    r"(9X Tashan|9X Jhakaas|Apna Punjab|Balle Balle|Ghaint|Pitaara|ABP Majha|"
    r"ABP Ananda|ABP Asmita|Saam TV|Zee Taas|Zee 24 Taas|Zee 24 Ghanta|"
    r"Zee 24 Kalak|Kaumudy|Tehzeeb|South Station|Zee South Flix|Northeast Live|"
    r"Hornbill|DD Urdu)", re.I)


def is_indian_regional(name: str) -> bool:
    """True for Indian regional-language channels (anything but Hindi/English)."""
    return bool(REGIONAL_LANG_RE.search(name) or REGIONAL_BRAND_RE.search(name))


# Per-genre fallback logo tiles (docs/logos/genre/, generated by
# gen_genre_logos.py) for channels with no tvg-logo and no iptv-org registry
# match, so no channel renders blank. Keyed by canonical genre; "TV" is default.
GENRE_LOGO_BASE = "https://raw.githubusercontent.com/wizakorhd/iptv/main/docs/logos/genre/"
GENRE_LOGO_SLUGS = {
    "news", "movies", "series", "entertainment", "general", "comedy", "reality",
    "sports", "documentary", "music", "kids", "food-travel", "horror", "sci-fi",
    "crime", "anime", "korean",
}


def genre_logo(group: str) -> str:
    genre = group.partition(" - ")[2].strip() if " - " in group else group.strip()
    slug = re.sub(r"[^a-z0-9]+", "-", genre.lower()).strip("-")
    if slug not in GENRE_LOGO_SLUGS:
        slug = "tv"
    return GENRE_LOGO_BASE + slug + ".png"


def canonical_group(group: str) -> str:
    """Fold stray region prefixes and granular genres into the consolidated
    scheme: '{Region} - {Genre}' (or a bare top-level label like 'Anime')."""
    if " - " not in group:
        return group
    region, _, genre = group.partition(" - ")
    region = REGION_CANON.get(region.strip().lower(), region.strip())
    genre = genre.strip()
    genre = GENRE_CANON.get(genre.lower(), genre)
    return f"{region} - {genre}"


def finalize(rows: list[dict]) -> list[dict]:
    """Sort rows, assign a stable sequential channel number (tvg-chno), and bake a
    global per-group channel count into the group title (Lume/other players don't
    show per-category counts on their own, e.g. 'Hindi - News (23)')."""
    for r in rows:
        r["group"] = canonical_group(r["group"])
    # fold singleton genres into '{Region} - Entertainment' to avoid 1-channel
    # clutter (e.g. a lone 'Hindi - Horror'); only affects '{Region} - {Genre}'.
    counts = Counter(r["group"] for r in rows)
    for r in rows:
        g = r["group"]
        if counts[g] == 1 and " - " in g:
            region = g.partition(" - ")[0]
            alt = f"{region} - Entertainment"
            if counts.get(alt, 0) >= 1:
                r["group"] = alt
    # fallback genre tile for logo-less channels so nothing renders blank.
    for r in rows:
        if not r.get("logo"):
            r["logo"] = genre_logo(r["group"])
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
def logo_by_name(db: dict) -> dict:
    """Map normalized channel name -> best logo URL from the iptv-org registry, so
    channels ingested from logo-less feeds (LG/Xiaomi apsattv lists) can still get a
    logo when the same channel exists in iptv-org's channel registry."""
    ch_logo: dict[str, str] = {}
    for lg in db["logos"]:
        cid = lg.get("channel")
        if not cid:
            continue
        if cid not in ch_logo or lg.get("in_use"):
            ch_logo[cid] = lg["url"]
    idx: dict[str, str] = {}
    for c in db["channels"]:
        url = ch_logo.get(c["id"])
        if not url:
            continue
        for nm in [c["name"]] + (c.get("alt_names") or []):
            k = norm_name(nm)
            if k and k not in idx:
                idx[k] = url
    return idx


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="re-download source data from iptv-org")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip stream reachability probing (faster, keeps dead/geo links)")
    ap.add_argument("--no-fast", action="store_true",
                    help="skip Samsung TV Plus (FAST) channels; use iptv-org only")
    ap.add_argument("--no-apsattv", action="store_true",
                    help="skip apsattv.com FAST playlists (LG/TCL/Vidaa/Distro/...)")
    ap.add_argument("--no-slate-check", action="store_true",
                    help="skip the Pluto slate-loop detection pass")
    ap.add_argument("--slate-delay", type=float, default=30.0,
                    help="seconds between the two Pluto slate-detection samples")
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

    if not args.no_fast:
        existing = {norm_name(c["name"]) for c in cands}
        fast = fast_candidates(existing, refresh=args.refresh)
        print(f"FAST (Samsung TV Plus) candidates: {len(fast)}", file=sys.stderr)
        cands += fast
        existing = {norm_name(c["name"]) for c in cands}
        roku = roku_candidates(existing, refresh=args.refresh)
        print(f"FAST (Roku) candidates: {len(roku)}", file=sys.stderr)
        cands += roku
        existing = {norm_name(c["name"]) for c in cands}
        pluto = pluto_candidates(existing, refresh=args.refresh)
        print(f"FAST (Pluto TV) candidates: {len(pluto)}", file=sys.stderr)
        cands += pluto

    if not args.no_apsattv:
        existing = {norm_name(c["name"]) for c in cands}
        apsat = apsattv_candidates(existing, refresh=args.refresh)
        print(f"FAST (apsattv) candidates: {len(apsat)}", file=sys.stderr)
        cands += apsat

    # backfill logos from the iptv-org registry for logo-less candidates (many
    # apsattv LG/Xiaomi feeds ship no tvg-logo).
    logo_idx = logo_by_name(db)
    filled = 0
    for c in cands:
        if not c.get("logo"):
            u = logo_idx.get(norm_name(c["name"]))
            if u:
                c["logo"] = u
                filled += 1
    print(f"Backfilled {filled} logos from iptv-org registry", file=sys.stderr)

    # drop Indian regional-language channels (keep only Hindi + English); does
    # not touch Hindi-belt state channels (DD Bihar, News18 Rajasthan, ...) which
    # broadcast in Hindi. See is_indian_regional().
    before = len(cands)
    cands = [c for c in cands if not is_indian_regional(c["name"])]
    print(f"Dropped {before - len(cands)} Indian regional-language channels",
          file=sys.stderr)

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

        if not args.no_slate_check:
            rows = drop_pluto_slates(rows, args.workers, args.timeout,
                                     delay=args.slate_delay)

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
