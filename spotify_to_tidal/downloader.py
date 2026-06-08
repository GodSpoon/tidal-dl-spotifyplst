"""Drive `tiddl` (https://github.com/oskvr37/tiddl) to download everything in a manifest.

We extract every `https://tidal.com/...` URL from the manifest and feed
them to `tiddl download url` in chunks. tiddl is preferred over the
older `tidal-dl` because it ships with a Tidal API key that the
current Tidal CDN accepts for playback URLs (the upstream tidal-dl

Backward-compatibility shim: the real implementation now lives in
``spotify_to_tidal/downloaders/tiddl.py``. This module re-exports every
public name so existing callers continue to work without changes.
"""
from __future__ import annotations

import subprocess  # noqa: F401 — exposed so tests can patch `spotify_to_tidal.downloader.subprocess.run`

from .downloaders.tiddl import (
    _RATE_LIMIT_RE,
    _which_tiddl,
    _which_tidal_dl,
    build_tidal_input_file,
    run_tidal_dl,
    _run_chunk_with_429_retry,
)
from .manifest import Manifest
from pathlib import Path


def print_summary(manifest: Manifest) -> None:
    s = manifest.stats
    if not s:
        manifest.compute_stats()
        s = manifest.stats
    print("\n========== Manifest Summary ==========")
    print(f"  Spotify user:        {manifest.spotify_user_id}")
    print(f"  Generated at:        {manifest.generated_at}")
    print(f"  Playlists:           {s.get('playlists', 0)}")
    print(
        f"  Playlist tracks:     {s.get('playlist_tracks', 0)} "
        f"({s.get('playlist_tracks_matched', 0)} matched, "
        f"{s.get('playlist_tracks_match_pct', 0):.1f}%)"
    )
    print(
        f"  Top artists:         {s.get('artists', 0)} "
        f"({s.get('artists_matched', 0)} matched)"
    )
    print(
        f"  Artist albums:       {s.get('artist_albums', 0)} "
        f"({s.get('artist_albums_matched', 0)} matched, "
        f"{s.get('artist_albums_match_pct', 0):.1f}%)"
    )
    print("======================================\n")


__all__ = [
    "_RATE_LIMIT_RE",
    "_which_tiddl",
    "_which_tidal_dl",
    "build_tidal_input_file",
    "run_tidal_dl",
    "_run_chunk_with_429_retry",
    "print_summary",
]
