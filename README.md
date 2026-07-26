# Curated IPTV — Hindi + Indian/Foreign English

Your own, self‑curated IPTV playlist and program guide (EPG), generated from the
free community [iptv-org](https://github.com/iptv-org) dataset.

## What's inside

| File | Purpose |
|------|---------|
| `playlist.m3u` | The curated channel list (point your player here). |
| `guide.xml` | XMLTV program guide (EPG). Channel ids match `playlist.m3u`. |
| `generate_playlist.py` | Builds `playlist.m3u` (curation + geo‑validation). |
| `generate_epg_config.py` | Builds `epg.channels.xml` for the EPG grabber. |
| `epg.channels.xml` | Guide‑source config consumed by `build_epg.sh`. |
| `build_epg.sh` | Grabs `guide.xml` via iptv-org/epg. |
| `curated_ids.json` | The final channel ids (playlist ↔ EPG link). |
| `Makefile` | Convenience targets (`make all`, `make playlist`, `make epg`). |

## Curation rules

- **Hindi** — all categories (any channel with a Hindi language feed).
- **English (India)** — all categories (Indian channels with an English feed).
- **English (International)** — **movies, entertainment, sports, news** only.

Every stream is **probed from the machine that runs the script**, so dead links
and streams that are **geo‑blocked from your location (i.e. would need a VPN)**
are dropped automatically. Run the playlist step **from India** to get the
correct "no‑VPN" result. NSFW and blocklisted channels are always excluded.

Channels are grouped as `Hindi - <Category>`, `English (India) - <Category>`,
`English (Intl) - <Movies|Entertainment|Sports|News>`.

## Build / refresh locally

```bash
python3 generate_playlist.py     # -> playlist.m3u (+ curated_ids.json), geo-validated
python3 generate_epg_config.py   # -> epg.channels.xml
./build_epg.sh 15 2              # -> guide.xml  (needs node 20)
# or simply:
make all
```

Useful flags: `--refresh` (re‑download source data), `--no-validate` (skip
probing), `--timeout`, `--workers`, `--max-try`.

## Hosting (free, via GitHub)

This repo is meant to be pushed to a **public** GitHub repo. Raw file URLs then
become your permanent playlist/EPG links (public is important — most Apple TV
players can't send an auth header for a private repo):

```
Playlist:  https://raw.githubusercontent.com/<user>/<repo>/main/playlist.m3u
EPG:       https://raw.githubusercontent.com/<user>/<repo>/main/guide.xml
```

### Auto‑refresh model
- **EPG** (`guide.xml`) changes daily and is location‑independent, so
  `.github/workflows/refresh-epg.yml` rebuilds it every day on GitHub's runners.
- **Playlist** (`playlist.m3u`) is geo‑validated and must be regenerated **from
  your location**. Re‑run `make playlist` locally and push when you want to
  re‑curate (channels/streams change slowly).

## Using it on your devices

All you need is an IPTV player that accepts an **m3u URL** + an **XMLTV EPG URL**:

- **Apple TV** — iPlayTV (~$5 one‑time) or GSE Smart IPTV, or the Channels app.
  Add the playlist URL, then set the EPG/XMLTV URL to `guide.xml`.
- **Mac / PC** — [Jellyfin](https://jellyfin.org) Live TV (free, best EPG UI):
  add an M3U Tuner (playlist URL) + an XMLTV guide source (EPG URL). VLC also
  plays the playlist but has no real guide UI.

The playlist header already contains `x-tvg-url="guide.xml"`, so players that
auto‑load the EPG from the playlist will pick it up once both are hosted
side‑by‑side.

## Notes & attribution

- Data/metadata © the [iptv-org](https://github.com/iptv-org) project (MIT).
  This project only re‑arranges public metadata and links to publicly listed
  streams; it hosts no video.
- Stream availability is community‑maintained and changes often — re‑run
  `make playlist` if channels stop working.
