# spotify_to_tidal

End-to-end tool that turns your Spotify library into a downloadable
Tidal queue. It:

1. **Logs into Spotify** (OAuth PKCE, no client_secret exposure) and
   reads your playlists + top 500 artists.
2. **Logs into Tidal** by reusing your existing `tidal-dl` device-code
   session (used internally for the search/match stage).
3. **Matches** every Spotify track and album to a Tidal ID using a
   scored fuzzy matcher (ISRC + title + artist + duration + track count).
4. **Writes a manifest** as JSON (with a CSV export for inspection).
5. **Hands a list of Tidal URLs to [`tiddl`](https://github.com/oskvr37/tiddl)**
   which downloads everything in 16-bit/44.1 kHz FLAC (or the best
   quality your Tidal subscription allows).

## Prerequisites

- Python 3.10+
- [`tiddl`](https://github.com/oskvr37/tiddl) on `PATH` —
  `pipx install tiddl` (this is the downloader)
- [`tidal-dl`](https://github.com/exislow/tidal-dl) on `PATH` —
  `pipx install tidal-dl` (only used internally for the search/match
  stage's Tidal catalog calls; you do not need to interact with it)
- A Spotify app: <https://developer.spotify.com/dashboard>
- A `http://127.0.0.1:8888/callback` redirect URI registered in that
  app's settings (this is the only one the tool uses).
- A Tidal subscription (the device-code login is the same as
  `tidal-dl`'s — you authorize once in a browser, the token is cached).

## Setup

```bash
git clone <this repo>
cd tidal-dl-spotifyplst
cp .env.example .env
# edit .env with your Spotify client_id / client_secret
pip install -r requirements.txt
pipx install tiddl
pipx install tidal-dl
```

## First-time login (one time each)

```bash
# Spotify (browser PKCE)
python3 -m spotify_to_tidal login

# Tidal — for the search/match stage (uses tidal-dl internally)
python3 -m spotify_to_tidal tidal-login
#   (or, if tidal-dl isn't logged in yet: just run `tidal-dl` once)

# Tidal — for the download stage (uses tiddl, separate session)
tiddl auth login
```

## Run the full pipeline

```bash
python3 -m spotify_to_tidal run
```

This will:

- Pull all your Spotify playlists + tracks
- Pull your top 500 artists + all their albums
- Search Tidal for each track/album
- Write `output/manifest.json` and `output/manifest.csv`
- Write `output/tiddl-input.txt` (one Tidal URL per line)
- Invoke the chosen downloader (`tiddl` by default) in chunks

You can swap the downloader backend via `--downloader`:
```bash
python3 -m spotify_to_tidal run --downloader tidarr
python3 -m spotify_to_tidal run --downloader both   # tiddl + tidarr
python3 -m spotify_to_tidal run --downloader squidwtf  # headless qobuz.squid.wtf
python3 -m spotify_to_tidal run --downloader all    # all configured sources
```

To include Qobuz in the download, set `SOURCES=tidal,qobuz` (or `SOURCES=qobuz`) in `.env`
and provide `QOBUZ_APP_ID`. You can also pass `--sources` per invocation:
```bash
python3 -m spotify_to_tidal run --sources tidal,qobuz
```

Supported backends: `tiddl` (default), `tidarr` (REST API), `qobuz`,
`squidwtf` (headless qobuz.squid.wtf), `both` (tiddl + tidarr), and
`all` (every configured source).
## Run each stage independently
```bash
python3 -m spotify_to_tidal build   --manifest output/manifest.json
python3 -m spotify_to_tidal match   --manifest output/manifest.json
python3 -m spotify_to_tidal download --manifest output/manifest.json --downloader tidarr
# Tidal's API rate-limits (HTTP 429) some downloads mid-run; both
# `download` and `run` auto-retry those chunks with exponential backoff.
# Tunable via --max-429-retries (default 4) and --download-chunk-size
# (default 15). Re-running is essentially free — tiddl skips files it
# already wrote.
# inspect a manifest
python3 -m spotify_to_tidal show -v --manifest output/manifest.json
```

## Post-processing (optional)

Once downloads finish you can organise, transcode, and generate playlists:

```bash
# Move files from tiddl_downloads into your library (e.g., Navidrome/Plexamp)
python3 -m spotify_to_tidal organize

# Auto-import downloads into beets (MusicBrainz tagging + library organisation)
python3 -m spotify_to_tidal beets
# Or enable automatic beets import after every download:
#   USE_BEETS=true in .env
# Then run with --beets:
#   python3 -m spotify_to_tidal run --beets

# Transcode FLAC → MP3 with FFmpeg (Apple-Silicon-optimised V0 VBR by default)
python3 -m spotify_to_tidal transcode

# Mirror-transcode FLAC library to a parallel MP3 tree (keeps both formats)
# Set MP3_LIBRARY_DIR in .env, or pass --mirror explicitly:
python3 -m spotify_to_tidal transcode --mirror /mnt/music/mp3 --workers 0
# --workers 0 auto-detects CPU count for parallel encoding

# Generate .m3u8 playlists in the library directory
python3 -m spotify_to_tidal playlists
```

Enable automatic Git versioning of the library by setting
`VERSION_LIBRARY_WITH_GIT=true` in `.env`.

## Why so many subcommands?

Because matching 500 artists × ~10 albums is a lot of Tidal searches.
If the network drops or the threshold is too aggressive, you can rerun
just `match` with a fresh `manifest.json` and pick up where you left
off (matched entries are skipped).

## Configuration

Everything lives in `.env`:

| Var | Default | Notes |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | – | **required** |
| `SPOTIFY_CLIENT_SECRET` | – | **required** (used with PKCE for the token exchange) |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Must be in your Spotify app's Redirect URIs. |
| `OUTPUT_DIR` | `./output` | Manifest and tiddl input file land here. |
| `TIDAL_DOWNLOAD_DIR` | `./tiddl_downloads` | Where tiddl writes files. |
| `TIDAL_QUALITY` | `max` | One of `max`, `high`, `normal`, `low` (tiddl's vocabulary). |
| `LIBRARY_DIR` | – | Destination for organized music (e.g., `/Volumes/music` or `/mnt/music/flac`). |
| `LIBRARY_SCHEMA` | `{artist}/{album}/{track}…` | Path template for file organisation. |
| `MP3_LIBRARY_DIR` | – | Mirror-transcode destination (e.g., `/mnt/music/mp3`). |
| `TRANSCODE_TO_MP3` | `false` | After download, transcode FLAC → MP3 via FFmpeg. |
| `DELETE_SOURCE_AFTER_TRANSCODE` | `false` | Remove original lossless files after transcoding. |
| `TRANSCODE_WORKERS` | `1` | Parallel transcoding threads. `0` = auto (CPU count). |
| `VERSION_LIBRARY_WITH_GIT` | `false` | Auto-commit library changes in a local git repo. |
| `USE_BEETS` | `false` | Auto-import downloads into beets after each run. |
| `DOWNLOADER` | `tiddl` | Backend: `tiddl`, `tidarr`, `qobuz`, `squidwtf`, `both`, or `all`. |
| `TIDARR_URL` | `http://localhost:8484` | Base URL of a running Tidarr instance. |
| `TIDARR_API_KEY` | – | API key from Tidarr settings (required when using `tidarr`). |
| `SOURCES` | `tidal` | Comma-separated sources to enable: `tidal`, `qobuz`. |
| `QOBUZ_APP_ID` | – | Qobuz application ID (required when `qobuz` in `SOURCES`). |
| `QOBUZ_AUTH_TOKEN` | – | Qobuz auth token (optional; obtained via Qobuz login flow). |

## Tidarr management commands

If you use the `tidarr` downloader (or just keep a Tidarr instance around),
these commands talk directly to the Tidarr REST API:

```bash
# List sync items
python3 -m spotify_to_tidal tidarr-sync-list

# Add a playlist/artist/album to the auto-sync list
python3 -m spotify_to_tidal tidarr-sync-add https://listen.tidal.com/playlist/abc123 --title "My Playlist"

# Remove a sync item by id
python3 -m spotify_to_tidal tidarr-sync-remove abc123

# Trigger an immediate sync run
python3 -m spotify_to_tidal tidarr-sync-trigger

# View download history
python3 -m spotify_to_tidal tidarr-history

# View or clear the download queue
python3 -m spotify_to_tidal tidarr-queue
python3 -m spotify_to_tidal tidarr-queue --clear
```

## Manifest format

JSON. Top-level:

```json
{
  "version": 1,
  "generated_at": "...",
  "spotify_user_id": "...",
  "playlists": [ PlaylistEntry, ... ],
  "artists":   [ ArtistEntry,   ... ],
  "stats":     { ... }
}
```

Each `TrackEntry` keeps the Spotify metadata **and** the matched
Tidal `id`, `match_score`, and human-readable `match_reasons` like:

```json
{
  "name": "Yesterday",
  "artists": ["The Beatles"],
  "isrc": "GBUM71505080",
  "tidal_id": 253822017,
  "match_score": 100.0,
  "match_reasons": ["isrc"]
}
```

Unmatched items get `"matched": false` and an `"error"` string
explaining why (`"best score 42.0 below threshold 55"`,
`"no candidates"`, etc.).

## Why a custom matcher?

Spotify and Tidal don't share catalog IDs. The only reliable join key
is **ISRC** for tracks (and we use that when present), but the Tidal
search API doesn't return ISRCs in the listing payload, so we still
have to do a search → score → pick-best pipeline. The scoring
function weights:

- ISRC exact match: +100
- Title exact match: +50, fuzzy: up to +35
- Artist match (primary or featured): up to +40
- Duration within 1.5s: +20, within 6s: +8, off by 15+s: -10
- Year + track-count sanity checks for albums

Thresholds are tunable at the top of `spotify_to_tidal/matcher.py`.

## Caveats

- **Compilation / "appears_on" albums**: matched but with a lower
  weight. Disable with `--no-compilations` on `build`/`run` if you
  don't want them.
- **Local files** in Spotify playlists are silently skipped
  (Tidal has no concept of them).
- **Playlists >500 tracks** are paginated correctly; the manifest
  stores the full list.
- **"Top artists" semantics**: Spotify's `/me/top/artists` returns at
  most ~50 per time-range. We union `long_term`, `medium_term`, and
  `short_term` and dedupe, which usually yields 200-500 distinct
  artists. If you want more, change the call in `spotify.py`.
- **Region / availability**: Tidal search can return tracks not
  available in your country. `tiddl` itself will skip those at
  download time.

## File layout

```
spotify_to_tidal/
├── __init__.py
├── __main__.py        # python -m spotify_to_tidal
├── cli.py             # argparse subcommands
├── config.py          # .env loading, paths
├── auth.py            # Spotify OAuth PKCE
├── spotify.py         # Spotify Web API client
├── tidal.py           # Tidal search via tidal-dl
├── qobuz.py           # Qobuz API client
├── matcher.py         # Spotify→Tidal/Qobuz scoring
├── manifest.py        # JSON/CSV manifest
├── downloader.py      # backward-compat shim
├── downloaders/       # modular downloader backends
│   ├── __init__.py
│   ├── factory.py
│   ├── tiddl.py
│   ├── tidarr.py
│   └── qobuz.py
├── tidarr_mgmt.py     # Tidarr REST API helpers
├── organizer.py       # library file organisation
├── playlists.py       # .m3u8 generation
├── transcode.py       # FLAC → MP3
├── git_version.py     # library git versioning
└── pipeline.py        # stage orchestration

tests/
├── test_manifest_and_downloader.py
├── test_matcher.py
├── test_multi_source.py
├── test_none_artist_regression.py
└── test_spotify_none_handling.py
```

## License

MIT.
