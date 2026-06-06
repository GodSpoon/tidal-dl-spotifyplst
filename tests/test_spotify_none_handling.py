"""Regression tests: Spotify API returns null for some array fields.

`dict.get(key, default)` only substitutes the default when the key is
absent, so the code must explicitly coalesce `None` for fields that
Spotify is known to return as `null` (notably `images` on user-owned
playlists when the owner hasn't set cover art).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotify_to_tidal.spotify import (  # noqa: E402
    SpotifyAlbum,
    SpotifyArtist,
    SpotifyClient,
    SpotifyTrack,
)


def test_artist_handles_none_genres_and_images():
    a = SpotifyClient._artist(
        {"id": "1", "name": "X", "uri": "spotify:artist:1",
         "genres": None, "images": None}
    )
    assert a.genres == []
    assert a.images == []


def test_album_handles_none_artists_and_images():
    a = SpotifyClient._album(
        {"id": "1", "name": "X", "uri": "spotify:album:1",
         "artists": None, "images": None}
    )
    assert a.artists == []
    assert a.images == []


def test_track_handles_none_artists():
    t = SpotifyClient._track(
        {"id": "1", "name": "X", "uri": "spotify:track:1",
         "duration_ms": 1000, "artists": None}
    )
    assert t.artists == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
