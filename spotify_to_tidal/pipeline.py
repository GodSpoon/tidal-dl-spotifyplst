"""End-to-end pipeline orchestration.

Composes: Spotify client → manifest → Tidal matcher → downloader.

The pipeline is split into stages so each can be re-run independently:

  build     - Fetch Spotify data, write manifest.
  match     - Read manifest, fill in Tidal IDs.
  download  - Run tidal-dl on the manifest.
  all       - build + match + download, in one go.
"""
from __future__ import annotations

from pathlib import Path

from .auth import get_token
from .config import AppConfig
from .downloader import build_tidal_input_file, run_tidal_dl, print_summary
from .manifest import (
    Manifest,
    album_from_spotify,
    artist_from_spotify,
    new_manifest,
    playlist_summary,
    track_from_spotify,
)
from .matcher import (
    match_album,
    match_artist,
    TRACK_MATCH_THRESHOLD,
    ALBUM_MATCH_THRESHOLD,
    ARTIST_MATCH_THRESHOLD,
)
from .spotify import SpotifyClient
from .tidal import TidalClient, ensure_tidal_logged_in


# ----------------- STAGE 1: build manifest -----------------

def build_manifest(
    cfg: AppConfig,
    *,
    top_artists_total: int = 500,
    include_artist_albums: bool = True,
    include_compilations: bool = True,
    progress=None,
) -> Manifest:
    """Pull all Spotify data into a fresh Manifest."""
    print("[1/3] Authenticating with Spotify…")
    token = get_token(cfg)
    sc = SpotifyClient(token)
    user_id = sc.current_user_id()
    print(f"      Spotify user: {user_id}")

    m = new_manifest(user_id)

    # Playlists
    print("[1/3] Fetching playlists…")
    summaries = sc.list_user_playlists(user_id)
    print(f"      Found {len(summaries)} playlists.")
    for i, ps in enumerate(summaries, 1):
        if progress:
            progress(i, len(summaries), ps.name)
        print(f"      [{i:>3}/{len(summaries)}] {ps.name} ({ps.track_count} tracks)")
        pl_full = sc.get_playlist(ps.id)
        entry = playlist_summary(ps)
        for t in pl_full.tracks:
            entry.tracks.append(track_from_spotify(t))
        m.playlists.append(entry)

    # Top artists
    print(f"[1/3] Fetching top {top_artists_total} artists…")
    top = sc.list_top_artists(top_artists_total)
    print(f"      Got {len(top)} unique artists.")
    for i, sa in enumerate(top, 1):
        if progress:
            progress(i, len(top), sa.name)
        ae = artist_from_spotify(sa)
        if include_artist_albums:
            groups = "album,single"
            if include_compilations:
                groups += ",compilation,appears_on"
            try:
                albums = sc.list_artist_albums(sa.id, include_groups=groups)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"      [!] Failed listing albums for {sa.name}: {e}")
                albums = []
            for al in albums:
                ae.albums.append(album_from_spotify(al))
        m.artists.append(ae)
        if i % 25 == 0 or i == len(top):
            print(
                f"      [{i:>3}/{len(top)}] {sa.name} "
                f"({len(ae.albums)} albums)"
            )

    m.compute_stats()
    return m


# ----------------- STAGE 2: match to Tidal -----------------

def match_manifest(
    cfg: AppConfig,
    manifest: Manifest,
    *,
    include_artist_albums: bool = True,
    include_playlists: bool = True,
    autosave_path: Path | None = None,
    autosave_every: int = 50,
) -> Manifest:
    ensure_tidal_logged_in()
    tc = TidalClient()
    print("      Tidal OK.")

    # Playlists
    if include_playlists:
        for pi, pl in enumerate(manifest.playlists, 1):
            print(f"      Playlist {pi}/{len(manifest.playlists)}: {pl.name}")
            for ti, t in enumerate(pl.tracks, 1):
                if t.matched and t.tidal_id:
                    continue  # already matched (re-runs)
                primary_artist = t.artists[0] if t.artists else ""
                query = f"{t.name} {primary_artist}".strip()
                try:
                    cands = tc.search_tracks(query, limit=10)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    t.error = f"search error: {e}"
                    continue
                # ISRC pre-filter
                if t.isrc:
                    for c in cands:
                        if c.isrc and c.isrc.upper() == t.isrc.upper():
                            t.tidal_id = c.id
                            t.tidal_title = c.title
                            t.tidal_artist = c.artist
                            t.tidal_album = c.album
                            t.tidal_album_id = c.album_id
                            t.tidal_duration = c.duration
                            t.match_score = 100.0
                            t.match_reasons = ["isrc"]
                            t.matched = True
                            break
                if not t.matched:
                    best, info = match_track_from_spotify_to_tidal(t, cands)
                    if best and info.score >= TRACK_MATCH_THRESHOLD:
                        t.tidal_id = best.id
                        t.tidal_title = best.title
                        t.tidal_artist = best.artist
                        t.tidal_album = best.album
                        t.tidal_album_id = best.album_id
                        t.tidal_duration = best.duration
                        t.match_score = round(info.score, 1)
                        t.match_reasons = info.reasons
                        t.matched = True
                    else:
                        t.error = (
                            f"best score {round(info.score, 1)} below threshold "
                            f"{TRACK_MATCH_THRESHOLD}"
                        ) if best else "no candidates"
                if ti % 50 == 0:
                    print(f"        …{ti}/{len(pl.tracks)}")
                if autosave_path and (ti % autosave_every == 0 or ti == len(pl.tracks)):
                    manifest.save_json(autosave_path)

    # Artists + albums
    for ai, ar in enumerate(manifest.artists, 1):
        if ar.matched and ar.tidal_id:
            cand_artists = []
        else:
            cand_artists = tc.search_artists(ar.name, limit=5)
            best_a, info_a = _match_artist(ar, cand_artists)
            if best_a and info_a.score >= ARTIST_MATCH_THRESHOLD:
                ar.tidal_id = best_a.id
                ar.tidal_name = best_a.name
                ar.match_score = round(info_a.score, 1)
                ar.matched = True
            else:
                ar.error = (
                    f"best score {round(info_a.score, 1)} below threshold"
                    if best_a else "no candidates"
                )
                # Don't even try to match albums for unmatched artists
                if include_artist_albums:
                    for al in ar.albums:
                        al.error = "artist unmatched"
                continue
        if ai % 10 == 0 or ai == len(manifest.artists):
            print(
                f"      Artist {ai}/{len(manifest.artists)}: {ar.name} "
                f"-> Tidal {ar.tidal_id} ({len(ar.albums)} albums)"
            )
        if autosave_path and (ai % autosave_every == 0 or ai == len(manifest.artists)):
            manifest.save_json(autosave_path)

        if include_artist_albums:
            # Pull the canonical Tidal discography for this artist
            tidal_discography = tc.get_artist_albums(ar.tidal_id, include_ep=True)
            for al in ar.albums:
                if al.matched and al.tidal_id:
                    continue
                best, info = match_album(al, tidal_discography)
                if best and info.score >= ALBUM_MATCH_THRESHOLD:
                    al.tidal_id = best.id
                    al.tidal_title = best.title
                    al.tidal_artist = best.artist
                    al.tidal_release_date = best.release_date
                    al.tidal_num_tracks = best.num_tracks
                    al.match_score = round(info.score, 1)
                    al.match_reasons = info.reasons
                    al.matched = True
                else:
                    al.error = (
                        f"best score {round(info.score, 1)} below threshold"
                        if best else "no candidates"
                    )

    manifest.compute_stats()
    return manifest


def _match_artist(ar, cand):
    """Thin wrapper to keep the match call shape symmetric."""
    from .matcher import match_artist as _ma
    return _ma(ar, cand)


def match_track_from_spotify_to_tidal(t, cands):
    """Build a transient SpotifyTrack-like for the matcher."""
    from .matcher import match_track as _mt
    from .spotify import SpotifyTrack, SpotifyArtist, SpotifyAlbum

    sp_track = SpotifyTrack(
        id=t.spotify_id,
        uri=t.spotify_uri,
        name=t.name,
        duration_ms=t.duration_ms,
        artists=[SpotifyArtist(id="", name=n, uri="", genres=[], popularity=0, followers=0, images=[])
                 for n in t.artists],
        album=SpotifyAlbum(
            id=t.album_id,
            name=t.album,
            uri="",
            album_type="album",
            artists=[],
            total_tracks=0,
            release_date="",
            images=[],
        ) if t.album else None,
        isrc=t.isrc,
        explicit=t.explicit,
    )
    return _mt(sp_track, cands)


# ----------------- STAGE 3: download -----------------

def download_from_manifest(
    cfg: AppConfig,
    manifest: Manifest,
    *,
    input_filename: str = "tiddl-input.txt",
    chunk_size: int = 100,
    max_429_retries: int = 4,
) -> Path:
    input_path = cfg.output_dir / input_filename
    track_n, album_n = build_tidal_input_file(manifest, input_path)
    print(f"[3/3] Wrote {track_n} tracks + {album_n} albums to {input_path}")
    rc = run_tidal_dl(
        input_path,
        output_dir=cfg.tidal_download_dir,
        quality=cfg.tidal_quality,
        chunk_size=chunk_size,
        max_429_retries=max_429_retries,
    )
    if rc != 0:
        print(f"[!] tiddl exited with status {rc}.")
    return input_path
def run_all(
    cfg: AppConfig,
    manifest_path: Path,
    *,
    top_artists_total: int = 500,
    include_artist_albums: bool = True,
    include_playlists: bool = True,
    skip_download: bool = False,
    download_chunk_size: int = 100,
    download_max_429_retries: int = 4,
) -> Manifest:
    if manifest_path.exists():
        print(f"[i] Loading existing manifest from {manifest_path}")
        m = Manifest.load_json(manifest_path)
    else:
        m = build_manifest(
            cfg,
            top_artists_total=top_artists_total,
            include_artist_albums=include_artist_albums,
        )
        m.save_json(manifest_path)
        csv_path = manifest_path.with_suffix(".csv")
        m.save_csv(csv_path)
        print(f"[i] Saved manifest to {manifest_path} and {csv_path}")

    m = match_manifest(
        cfg, m,
        include_artist_albums=include_artist_albums,
        include_playlists=include_playlists,
        autosave_path=manifest_path,
    )
    m.save_json(manifest_path)
    print_summary(m)

    if not skip_download:
        download_from_manifest(
            cfg, m,
            chunk_size=download_chunk_size,
            max_429_retries=download_max_429_retries,
        )
    return m
