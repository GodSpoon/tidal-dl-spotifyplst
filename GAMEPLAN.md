# Spotify-to-Tidal Gameplan

> Generated 2026-06-06 from a comprehensive codebase audit + feature implementation sprint.
> Branch: `feat/codebase-improvements`

---

## 1. What Was Done (Audit + Implementation)

### 1.1 Code Quality Fixes (Commit `e794b3d`)

| Issue | File | Fix |
|-------|------|-----|
| `None` API-key index persisted to config, corrupting tidal-dl settings | `tidal.py` | Guard: `if chosen is not None and chosen != SETTINGS.apiKeyIndex` |
| Dead `if False else` ternary left in pipeline | `pipeline.py` | Removed; direct call to `_match_artist` |
| Downloader docstring claimed "last chunk's rc" but logic preserved any non-zero | `downloader.py` | Docstring corrected to "0 if all succeeded, else most recent non-zero" |
| Spotify 429 exhaustion raised `RuntimeError(... failed: None)` | `spotify.py` | Set `last_exc = RuntimeError(f"Rate limited (Retry-After: {wait}s)")` in 429 branch |
| Failed Tidal searches cached empty result permanently | `tidal.py` | `return []` without writing to `_search_cache` |
| Failed `getArtistAlbums` cached empty result permanently | `tidal.py` | `return []` without writing to `_albums_by_artist` |
| `KeyboardInterrupt` swallowed by bare `except Exception` | `pipeline.py` | Added `except KeyboardInterrupt: raise` before bare handlers |

### 1.2 New Features (Commit `f6a910b`)

#### Module: `organizer.py`
- **Purpose**: Move files from raw `tiddl_downloads` into a structured library directory.
- **Key types**: `TrackMeta`, `OrganizeConfig(schema=..., library_dir=..., copy=False)`, `OrganizeResult`
- **API**:
  - `organize_file(source, cfg, hint=None) -> OrganizeResult`
  - `organize_directory(source_dir, cfg) -> list[OrganizeResult]`
  - `organize_from_manifest(manifest, cfg) -> list[OrganizeResult]`
- **Schema templating**: `{artist}`, `{album}`, `{title}`, `{track_num}`, `{ext}`, `{year}` — values auto-sanitised for macOS/Windows filesystems.
- **Collision handling**: Skip if same size; append ` (1)`, ` (2)`... if different size.
- **Metadata reading**: Prefers `mutagen` if installed; falls back to manifest `TrackEntry` hint; falls back to filename heuristic.

#### Module: `playlists.py`
- **Purpose**: Generate `.m3u8` playlist files from the manifest.
- **API**:
  - `write_playlists(manifest, library_dir, output_dir=None) -> list[Path]`
  - `generate_m3u_playlists(manifest, library_dir) -> list[Path]` (alias)
- **Format**: `#EXTM3U` + `#EXTINF:{seconds},{Artist} - {Title}` + relative path from playlist dir.
- **File discovery**: Recursive `rglob` of library dir; fuzzy match by normalised stem. Missing tracks get `# missing:` comment.
- **Output**: Defaults to `{library_dir}/playlists/{sanitized_name}.m3u8`.

#### Module: `transcode.py`
- **Purpose**: Transcode lossless (FLAC) → MP3 via FFmpeg.
- **API**:
  - `transcode_file(src, dst, *, delete_source=False) -> bool`
  - `transcode_directory(root, *, delete_source=False, pattern="*.flac") -> tuple[int, int]` (ok, fail)
- **Apple Silicon**: Detects `Darwin` + `arm64` → uses `-c:a libmp3lame -q:a 0` (V0 VBR, ~245kbps). No hardware MP3 encoder exists on M-series; libmp3lame is NEON-optimised.
- **Skip logic**: If destination `.mp3` exists and `mtime >= src.mtime`, skip.
- **Metadata**: `-map_metadata 0 -id3v2_version 3` preserves tags.

#### Module: `git_version.py`
- **Purpose**: Safe Git versioning for the library directory.
- **API**:
  - `init_library_repo(library_dir) -> bool`
  - `commit_library_changes(library_dir, message) -> bool`
  - `has_uncommitted_changes(library_dir) -> bool`
  - `ensure_library_versioned(library_dir)` — high-level entry point
- **Safety**: All `subprocess.run(..., check=False)`; errors logged, never raised. `.gitignore` covers `*.tmp`, `*.part`, `.DS_Store`.

### 1.3 Behavioural Changes

- **Album-at-a-time downloads**: `build_tidal_input_file` now groups playlist tracks by `tidal_album_id`. Albums with ≥2 tracks emit `https://tidal.com/browse/album/{id}` once; singletons/album-less tracks still emit individual track URLs. Dramatically reduces download overhead.
- **Schema change**: `TrackEntry.tidal_album_id: Optional[int] = None` added to `manifest.py`. Populated in `pipeline.py` during ISRC pre-filter and score-match blocks from `TidalTrack.album_id`.
- **Config additions**:
  - `LIBRARY_DIR`, `LIBRARY_SCHEMA`
  - `TRANSCODE_TO_MP3`, `DELETE_SOURCE_AFTER_TRANSCODE`
  - `VERSION_LIBRARY_WITH_GIT`
- **CLI additions**: `organize`, `transcode`, `playlists` subcommands.

---

## 2. Architecture Assessment

### Strengths
- Clear module separation: cli / pipeline / spotify / tidal / matcher / manifest / downloader
- Explicit staged pipeline (build → match → download) with CLI 1:1 mapping
- Matcher is pure logic, well-tested
- Manifest is a robust serialisation boundary (JSON + CSV)
- Defensive null-handling in Spotify mappers

### Weaknesses
- `tidal.py` tightly coupled to `tidal_dl` global singleton (sys.path manipulation, token lifecycle inline)
- `pipeline.py` mutates `Manifest` in place and interleaves I/O (autosave) with business logic
- No interface abstractions for API clients → impossible to unit-test pipeline without real credentials
- Thresholds/scoring weights are module-level constants, not configurable
- `match_track_from_spotify_to_tidal` fabricates a fake `SpotifyTrack` from `TrackEntry` — data-model leakage
- CLI commands duplicate I/O logic (path resolution, save/load)
- Tests use `sys.path.insert` instead of proper package installation

---

## 3. Remaining Recommended Work

### High Impact / Low Effort
1. **Persistent API cache** — SQLite or JSON on disk for Tidal search results and Spotify responses with 30-day TTL. Eliminates re-run latency entirely.
2. **Parallelise matching** — `ThreadPoolExecutor` (8–16 workers) for `tc.search_tracks()` calls. The single biggest wall-clock bottleneck.
3. **Parallelise playlist fetching** — `ThreadPoolExecutor` for `get_playlist()` calls across playlists.

### High Impact / Medium Effort
4. **ABCs for API clients** — `SpotifyClientInterface` / `TidalClientInterface` so pipeline tests can inject fakes.
5. **Configurable matcher thresholds** — Move `TRACK_MATCH_THRESHOLD` etc. into `AppConfig` or a `MatcherConfig` dataclass.
6. **Album-at-a-time for artist albums** — Already done for playlists; artist album URLs already emit as albums. No further work needed here.

### High Impact / High Effort
7. **New-release monitor** — Scheduled job that polls top artists for new albums since last run. Requires date tracking and incremental manifest updates.
8. **Metadata repair pass** — Batch tag normalisation, artwork embedding, ISRC injection using `mutagen`.
9. **Replace tidal-dl dependency** — The upstream `tidal_dl` SDK is fragile (global singleton, API-key probing, token corruption). A direct Tidal API client (using their public OAuth2 + REST API) would be more reliable and testable. This is a large refactor.

### Medium Impact / Low Effort
10. **Add `--organize` / `--transcode` / `--playlists` flags to `run`** — So the full pipeline can optionally include post-processing in one command.
11. **Add `mypy` / `ruff` to dev dependencies** — Type-checking and linting for code quality gating.

---

## 4. Module Boundaries & Contracts

### `spotify.py`
- `SpotifyClient(token)` — concrete class with `requests.Session`
- `SpotifyClient._request()` — 5 retries with exponential backoff, respects `Retry-After`
- `SpotifyClient._paginate()` — yields items from paginated endpoints
- **Do NOT add business logic here.** Keep it a thin HTTP wrapper.

### `tidal.py`
- `TidalClient` — thin wrapper over `tidal_dl.tidal.TIDAL_API` with in-memory caching
- `_search_cache: dict[tuple[str,str], list]` — caches successful results only (fixed)
- `_albums_by_artist: dict[int, list[TidalAlbum]]` — caches successful results only (fixed)
- **Do NOT add more stateful logic here.** Extract `TidalAuthManager` if you need to refactor auth.

### `matcher.py`
- Pure functions: `match_track`, `match_album`, `match_artist`
- Input: Spotify dataclass + list of Tidal candidates
- Output: `(best_candidate, Match(score, reasons))`
- Thresholds are module-level constants; consider making them parameters.

### `manifest.py`
- Central data model: `Manifest`, `PlaylistEntry`, `ArtistEntry`, `AlbumEntry`, `TrackEntry`
- `TrackEntry` now has `tidal_album_id: Optional[int]` — **future agents must preserve this field** in any refactor.
- `Manifest.save_json()` / `Manifest.load_json()` — serialisation boundary

### `pipeline.py`
- `build_manifest(cfg)` → `Manifest`
- `match_manifest(cfg, manifest)` → mutated `Manifest`
- `download_from_manifest(cfg, manifest)` → writes tiddl input file
- `run_all(cfg, manifest_path)` → orchestrates all three
- **Note**: `match_manifest` does autosave every N items; this is the right place to add parallelism.

### `downloader.py`
- `build_tidal_input_file(manifest, out_path)` → writes `.txt` for tiddl
- `run_tidal_dl(input_file, ...)` → chunks URLs into `tiddl download url` subprocess calls
- Chunk size and 429 retry are configurable via CLI

---

## 5. Config & Env Var Reference

| Var | Type | Default | Description |
|-----|------|---------|-------------|
| `SPOTIFY_CLIENT_ID` | str | **required** | Spotify app client ID |
| `SPOTIFY_CLIENT_SECRET` | str | **required** | Spotify app client secret |
| `SPOTIFY_REDIRECT_URI` | str | `http://127.0.0.1:8888/callback` | Must match app settings |
| `OUTPUT_DIR` | Path | `./output` | Manifest + tiddl input file |
| `TIDAL_DOWNLOAD_DIR` | Path | `./tiddl_downloads` | Raw downloads from tiddl |
| `TIDAL_QUALITY` | str | `max` | `max`/`high`/`normal`/`low` |
| `LIBRARY_DIR` | Path | `None` | Destination for organised library |
| `LIBRARY_SCHEMA` | str | `{artist}/{album}/{track_num:02d} - {title}.{ext}` | Path template |
| `TRANSCODE_TO_MP3` | bool | `false` | Enable FLAC→MP3 post-processing |
| `DELETE_SOURCE_AFTER_TRANSCODE` | bool | `false` | Remove FLAC after MP3 created |
| `VERSION_LIBRARY_WITH_GIT` | bool | `false` | Auto-commit library changes |

---

## 6. Testing Strategy

- Unit tests for `matcher.py` are comprehensive and should be kept.
- Unit tests for `manifest.py` cover round-trip serialisation.
- **Missing**: pipeline orchestration tests, auth flow tests, CLI tests.
- **Blocker for pipeline tests**: No interface abstraction for `SpotifyClient` / `TidalClient`.
- Recommendation: Add `pytest` fixtures with `unittest.mock` patches for `_request` / `_search` if you need pipeline tests before extracting ABCs.

---

## 7. Git Workflow

- All work is on branch `feat/codebase-improvements`.
- Commit 1 (`e794b3d`): pure fixes, no behavioural changes.
- Commit 2 (`f6a910b`): new features + schema expansion + CLI wiring.
- **Before merging to `main`:**
  - Rebase or squash if desired
  - Ensure `.claude/` cache directory is in `.gitignore`
  - Run `python3 -m pytest tests/ -v`

---

## 8. Quick Reference for Future Agents

**Adding a new CLI subcommand:**
1. Add `cmd_*` function in `cli.py`.
2. Register it in `build_parser()` with `sub.add_parser(...)`.
3. If it needs config, add field to `AppConfig` + env var + `.env.example` + `README.md`.
4. Wire imports at top of `cli.py`.

**Adding a new config field:**
1. Add to `AppConfig` dataclass in `config.py`.
2. Add env var name to `_load_env()` loop.
3. Parse it in `load_config()`.
4. Update `.env.example` and `README.md` config table.

**Modifying the manifest schema:**
1. Add field to the dataclass in `manifest.py`.
2. Update the `from_dict` / `to_dict` helper for that type.
3. Update `*_from_spotify()` builder if the data is available from Spotify.
4. Update pipeline code that populates the field from Tidal results.
5. Update tests that construct the dataclass directly.

**Using the new post-processing modules:**
```python
from spotify_to_tidal import organizer, playlists, transcode, git_version

# Organize
cfg = organizer.OrganizeConfig(
    library_dir=Path("/Volumes/media/Audio/Music"),
    schema="{artist}/{album}/{track_num:02d} - {title}.{ext}",
    copy=False,
)
results = organizer.organize_from_manifest(manifest, cfg)

# Playlists
written = playlists.write_playlists(manifest, cfg.library_dir)

# Transcode
ok, fail = transcode.transcode_directory(cfg.library_dir)

# Git version
git_version.ensure_library_versioned(cfg.library_dir)
git_version.commit_library_changes(cfg.library_dir, "message")
```
