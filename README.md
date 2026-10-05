# ORPHEUS — a local-first sound system

> One server on your LAN, pointed at a folder of music you own. Every screen
> on your WiFi becomes a speaker or a remote. Speakers are **stars in a
> constellation** — drag them, tune them, give each one a role (bass / mids /
> highs) and build a surround rig out of whatever hardware you own. True
> sample-locked multi-room sync (~1 ms), nothing fetched from a streaming
> service, nothing leaves the LAN.

![Python](https://img.shields.io/badge/python-3.11+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-async-009688) ![Snapcast](https://img.shields.io/badge/sync-Snapcast-orange) ![No build step](https://img.shields.io/badge/frontend-vanilla%20JS%2C%20no%20build-yellow) ![License](https://img.shields.io/badge/license-MIT-green)

Built as a personal project; published as a portfolio piece. It is a
working system, not a demo — it runs my living room.

## Highlights

- **The Sky** — party speakers render as stars: size = volume, color = role,
  dashed constellation lines, drag-to-arrange, tap-to-tune. SVG, no framework.
- **Real multi-room sync** via [Snapcast](https://github.com/badaix/snapcast):
  one server chops audio into timestamped chunks; every client plays each
  chunk at its scheduled instant. Native clients hold ~1 ms alignment.
- **Frequency-band roles** — one `ffmpeg` decode is split four ways
  (FULL / THUMP ≤160 Hz / HEART 160 Hz–4 kHz / AIR ≥4 kHz). A speaker
  subscribes to a band and *becomes* the subwoofer or the tweeter. Because
  all bands share one decode clock they cannot drift apart.
- **Any audio device becomes a speaker** — Bluetooth speakers, HDMI, wired:
  each gets its own pinned client, joinable from the UI.
- **Clock discipline** — WSL2's VM clock was measured drifting 7.5 ms/s,
  enough to glitch sync. A chrony sidecar container + host NTP fix brought
  it under 1 ms/s. Documented in `party/chrony.conf`.
- **Your files are the catalog** — point `ORPHEUS_LIBRARY` at a folder; the
  scanner reads tags and duration with `ffprobe`, pulls embedded cover art out
  with `ffmpeg`, and indexes it all in SQLite. Search, albums, artists,
  song-radio and the home shelves are built from that index and your own
  play history. Range-aware streaming so every phone on the LAN can seek.
- **Local-first everything** — plays, index, playlists and settings live in
  one SQLite file on your machine. Nothing phones home.
- Plus: Spotify playlists imported as metadata and matched against your
  files (official OAuth), Spotify Connect as a solo node, 30k live radio
  stations, synced lyrics, a QR guest jukebox, six themes.

## Architecture

```
                 ┌──────────────── Orpheus server (FastAPI, :7777) ────────────────┐
  phones/tabs ◀──┤  constellation.py  WebSocket hub — state, nodes, deck control    │
  (remotes)      │  library.py        folder scanner + index + range-aware streams  │
                 │  party.py          Snapcast feeder + JSON-RPC control + roles    │
                 │  sources.py / frame.py / stations.py / lyrics.py / guest.py ...  │
                 └───────────────────────────┬────────────────────────────────────┘
                                             │ one ffmpeg, 4 PCM outputs (tcp)
                                             ▼
                 ┌──────── snapserver (Docker) ── streams: FULL THUMP HEART AIR ────┐
                 │  + chrony sidecar: keeps the VM clock honest                     │
                 └───────────────────────────┬────────────────────────────────────┘
                              timestamped chunks, one shared clock
              ┌──────────────┬───────────────┼────────────────┬──────────────┐
         snapclient      snapclient      snapclient        snapweb        snapweb
        (PC speakers)   (BT speaker A)  (BT speaker B)   (phone tab)    (laptop tab)
          ~1 ms            ~1 ms           ~1 ms           ~±50 ms        ~±50 ms
```

## Quick start (Windows)

Prereqs: **Python 3.11+**, **Docker Desktop**, **ffmpeg** on PATH.

```bat
git clone https://github.com/saifkhatatbeh/orpheus.git
cd orpheus
orpheus.bat
```

First run creates the venv, installs deps, downloads the Snapcast client +
web player (official releases, not vendored), then starts the server.

Drop your music into `library/` (or set `ORPHEUS_LIBRARY=D:\Music` before
starting). The folder is scanned on startup and whenever you hit **RESCAN** in
the FILES room; mp3, m4a, flac, ogg, opus, wav and aiff are picked up by tag.

- This PC: http://localhost:7777
- Phone / iPad: `http://<your-LAN-IP>:7777` (printed on startup, same WiFi)

Start the party engine once (it stays up across reboots):

```bat
docker compose -f party\docker-compose.yml up -d
```

First time only — allow LAN access through Windows Firewall (admin PowerShell):

```
netsh advfirewall firewall add rule name="Orpheus 7777" dir=in action=allow protocol=TCP localport=7777
```

<details>
<summary>Linux / macOS</summary>

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
docker compose -f party/docker-compose.yml up -d
python -m server.main
# native client: install `snapclient` from your package manager, run
#   snapclient -h 127.0.0.1 --hostID MY-PC
```
The chrony sidecar is only needed on Docker Desktop / WSL2 hosts.
</details>

## Tests

End-to-end against the live stack (server + snapserver must be running, and
at least one file in the library folder):

```bat
.venv\Scripts\python tests\test_party_e2e.py    # feed, seek, pause, next, volume, party off
.venv\Scripts\python tests\test_sky_bands.py    # all 4 bands live, role switching, teardown
```

## The four rooms

| Room | What it is |
|---|---|
| **DECK** | Now playing + queue + song-radio (∞, from your own shelves) + synced lyrics (LYR) + Rewind shelves from your own history |
| **LIBRARY** | Playlists imported from Spotify as metadata, each row matched to a file you own; rows with no match don't play |
| **RADIO** | 30k live stations (radio-browser), proxied through the server so everything plays |
| **FILES** | Everything in the library folder, indexed by tags, with a rescan button |

## Constellation (the NODES button)

Every open Orpheus tab registers as a node. Sound follows the *active* node —
tap a node to move playback there.

- **Edifier** = the PC tab (PC's Bluetooth out)
- **Phone / iPad** = their own tabs
- **Samsung Music Frame** = via Spotify Connect (single-node mode only, see below)
- **DLNA renderers** (TVs etc.) = SCAN WIFI / add by IP

## PARTY MODE (the Snapcast engine)

True multi-room sync — every joined speaker plays **sample-locked** (~1ms on
native clients). Local-first: one server on your LAN chops the deck's audio
into timestamped chunks; every client plays each chunk at its scheduled
instant against a shared clock.

```
deck track ──ffmpeg──▶ PCM 48k ──▶ snapserver (Docker) ──chunks──▶ all clients
```

**Setup (once):** `cd party && docker compose up -d` — runs snapserver +
chrony (the chrony container tames WSL2's clock drift, which would otherwise
glitch sync; drift was measured at 7.5ms/s on this box before, <1ms/s after).

**Joining speakers:**
- **This PC (Edifier)** — double-click `party/join-party-pc.bat`
- **Any phone/tablet/laptop** — NODES → PARTY MODE ✓ → **+ ADD SPEAKERS** →
  **THIS DEVICE** (opens snapweb, the synced web player — one tap, no app)
- **Bluetooth speakers** — pair them with the PC in Windows Bluetooth
  settings, then NODES → PARTY → **SCAN PC AUDIO DEVICES** → JOIN next to the
  speaker. Each one runs its own pinned snapclient. Add as many as Windows
  will hold pairings for.
- **Music Frame over Bluetooth** — same as any BT speaker: pair `Music Frame`
  in Windows, scan, JOIN. This is the *native* way to get the Frame into the
  party — see below for why WiFi can't do it.

### The Sky

Party speakers render as **stars in a night sky**. Drag stars to draw your
own constellation (layout saved per device). Star **size = volume**,
**color = role**. Tap a star to open its tuner:

- **BRIGHTNESS** — per-speaker volume
- **ROLE** — what the star sings. The feeder splits every track into four
  sample-locked bands (one ffmpeg, four outputs — they can't drift):
  | role | color | band | use it for |
  |---|---|---|---|
  | FULL | white | everything | normal speakers |
  | THUMP | ember | ≤160 Hz | the speaker with the biggest woofer |
  | HEART | acid | 160 Hz–4 kHz | vocals & instruments, your clearest speaker |
  | AIR | ice | ≥4 kHz | small bright speakers, sparkle |
  Assign THUMP to the Edifier and AIR to a small BT speaker and you've built
  a surround rig out of mismatched hardware.
- **TIMING TRIM** — per-speaker latency nudge (BT speakers ≈ −150ms)

Stars pulse while music plays; offline speakers dim to embers. Deck controls
(play, pause, seek, next) drive the whole sky at once.

## Music Frame (why WiFi = Spotify only)

Port + mDNS census of the Frame shows it advertises **only**
`_spotify-connect._tcp` — no AirPlay (:7000), no Chromecast (:8009), no DLNA,
no Samsung multiroom ports. AirPlay/Alexa/Tidal run *inside licensed,
encrypted stacks* (AirPlay requires Apple MFi keys baked into the chip);
there is no open LAN audio input on this device. Every open-source path in
existence (shairport-sync etc.) makes your PC *receive* AirPlay, not send to
a licensed speaker.

So the Frame has exactly two roles in Orpheus:

- **In the party** → join it over **Bluetooth** (pair with the PC, JOIN in the
  party panel). Real chunk-sync, no cloud, no Spotify.
- **Solo node (outside party)** → Spotify Connect, driven the way the Spotify
  app does it:
  1. LIBRARY → CONNECT SOURCES → **LOGIN WITH SPOTIFY** (OAuth, see Sources)
  2. NODES → **FIND CONNECT DEVICES** → Frame appears as a node
  3. Set it active → deck playback is mirrored to the Frame
     (track matched on Spotify, Premium required)

## Sources (Spotify, official OAuth)

One-time, the token stays in `orpheus.db` on this machine:

- developer.spotify.com/dashboard → Create app → set Redirect URI to
  `http://127.0.0.1:7777/api/sources/spotify/callback` (must be `127.0.0.1`, not
  `localhost`) → copy the **Client ID** → paste into Orpheus → **LOGIN WITH SPOTIFY**
  (Authorization Code + PKCE; no client secret, nothing scraped).

IMPORT pulls your playlists and liked songs into LIBRARY as *metadata*. Playback
matches each row to a file in your library folder by title, artist and
duration; a row with no local copy stays greyed out. The same playlists can be
played on a Spotify Connect device (the Frame) through the official API,
Premium required.

## Guest Pass

GUEST button → QR. Anyone on your WiFi scans it → minimal jukebox page →
search & queue (max 4/min, can't skip or stop). No app, no account.

## Themes

◐ button cycles: ULTRAMARINE · VOID · DARKROOM · TERMINAL · TANGERINE · RIVIERA.
Persists per device — phone can run TERMINAL while the PC runs ULTRAMARINE.

## Layout

```
server/   FastAPI: library.py scanner/index/streams · constellation.py WS hub ·
          party.py Snapcast feeder/roles · sources.py Spotify import + local
          matching · frame.py Spotify Connect driver · dlna.py SSDP+mDNS ·
          stations.py radio · lyrics.py · guest.py
web/      index.html + assets/ (no build step, vanilla JS + SVG)
party/    docker-compose.yml (snapserver + chrony) · snapserver.conf (4 band
          streams) · setup_party.py (fetches snapclient + snapweb) · join-party-pc.bat
tests/    end-to-end suites against the live stack
library/  your music (or set ORPHEUS_LIBRARY) — gitignored
.art/     cover art extracted from your files — gitignored
orpheus.db  plays, library index, playlists, settings — gitignored
```

## Privacy

Everything stays on your machine. The Spotify token is stored in
`orpheus.db`, gitignored. Nothing phones home; the only outbound traffic is
radio streams you tune to, lyrics lookups (lrclib.net) and the Spotify API
when you link an account.

## License

MIT — see [LICENSE](LICENSE). Snapcast and snapweb are © their authors
(GPL-3.0) and are downloaded at setup, not redistributed here.
