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
python -m spotify_to_tidal login

# Tidal — for the search/match stage (uses tidal-dl internally)
python -m spotify_to_tidal tidal-login
#   (or, if tidal-dl isn't logged in yet: just run `tidal-dl` once)

# Tidal — for the download stage (uses tiddl, separate session)
tiddl auth login
```

## Run the full pipeline

```bash
python -m spotify_to_tidal run
```

This will:

- Pull all your Spotify playlists + tracks
- Pull your top 500 artists + all their albums
- Search Tidal for each track/album
- Write `output/manifest.json` and `output/manifest.csv`
- Write `output/tiddl-input.txt` (one Tidal URL per line)
- Invoke `tiddl download -p tiddl_downloads -q max url` in chunks

## Run each stage independently

```bash
python -m spotify_to_tidal build   --manifest output/manifest.json
python -m spotify_to_tidal match   --manifest output/manifest.json
python -m spotify_to_tidal download --manifest output/manifest.json

# inspect a manifest
python -m spotify_to_tidal show -v --manifest output/manifest.json
```

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
├── matcher.py         # Spotify→Tidal scoring
├── manifest.py        # JSON/CSV manifest
├── downloader.py      # tiddl subprocess driver
└── pipeline.py        # stage orchestration

tests/
├── test_manifest_and_downloader.py
├── test_matcher.py
├── test_none_artist_regression.py
└── test_spotify_none_handling.py
```

## License

MIT.
