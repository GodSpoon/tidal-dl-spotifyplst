"""Tests for manifest serialization and tidal-dl input file generation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotify_to_tidal.manifest import (  # noqa: E402
    Manifest,
    TrackEntry,
    AlbumEntry,
    ArtistEntry,
    PlaylistEntry,
    new_manifest,
)
from spotify_to_tidal.downloader import build_tidal_input_file  # noqa: E402


def _sample_manifest() -> Manifest:
    m = new_manifest("alice")
    pl = PlaylistEntry(
        spotify_id="pl1",
        name="My Mix",
        owner="alice",
        description="hi",
        public=False,
        collaborative=False,
        track_count=2,
        tracks=[
            TrackEntry(
                spotify_id="t1", spotify_uri="spotify:track:t1",
                name="Yesterday", duration_ms=125000,
                artists=["The Beatles"], album="Help!", album_id="al1",
                isrc="GBUM71505080", explicit=False,
                tidal_id=253822017, tidal_title="Yesterday",
                tidal_artist="The Beatles", tidal_album="Help!",
                tidal_duration=125, match_score=100.0,
                match_reasons=["isrc"], matched=True,
            ),
            TrackEntry(
                spotify_id="t2", spotify_uri="spotify:track:t2",
                name="Unmatched Song", duration_ms=200000,
                artists=["Unknown"], album="?", album_id="al2",
                isrc="", explicit=False, matched=False,
                error="no candidates",
            ),
        ],
    )
    m.playlists.append(pl)
    a = ArtistEntry(
        spotify_id="a1", name="The Beatles",
        genres=["rock"], popularity=85, followers=10_000_000,
        tidal_id=2076, tidal_name="The Beatles",
        match_score=100.0, matched=True,
        albums=[
            AlbumEntry(
                spotify_id="al1", spotify_uri="spotify:album:al1",
                name="Help!", artists=["The Beatles"],
                album_type="album", total_tracks=14, release_date="1965-08-06",
                tidal_id=58138532, tidal_title="Help!",
                tidal_artist="The Beatles",
                tidal_release_date="1965-08-06", tidal_num_tracks=14,
                match_score=120.0, match_reasons=["title=50*", "artist=40"],
                matched=True,
            ),
        ],
    )
    m.artists.append(a)
    m.compute_stats()
    return m


def test_manifest_roundtrip(tmp_path: Path):
    m = _sample_manifest()
    out = tmp_path / "manifest.json"
    m.save_json(out)
    loaded = Manifest.load_json(out)
    assert loaded.spotify_user_id == "alice"
    assert loaded.playlists[0].name == "My Mix"
    assert loaded.playlists[0].tracks[0].tidal_id == 253822017
    assert loaded.playlists[0].tracks[1].matched is False
    assert loaded.artists[0].albums[0].tidal_id == 58138532


def test_manifest_csv_export(tmp_path: Path):
    m = _sample_manifest()
    out = tmp_path / "manifest.csv"
    m.save_csv(out)
    text = out.read_text()
    assert "My Mix" in text
    assert "The Beatles" in text
    assert "https://tidal.com/browse/track/253822017" in text
    assert "https://tidal.com/browse/album/58138532" in text
    # Unmatched track should appear with tidal_id empty and matched=no
    assert "Unmatched Song" in text


def test_manifest_stats_computed():
    m = _sample_manifest()
    assert m.stats["playlists"] == 1
    assert m.stats["playlist_tracks"] == 2
    assert m.stats["playlist_tracks_matched"] == 1
    assert m.stats["playlist_tracks_match_pct"] == 50.0
    assert m.stats["artists"] == 1
    assert m.stats["artists_matched"] == 1
    assert m.stats["artist_albums"] == 1
    assert m.stats["artist_albums_matched"] == 1
    assert m.stats["artist_albums_match_pct"] == 100.0


def test_tidal_input_file_format(tmp_path: Path):
    m = _sample_manifest()
    out = tmp_path / "tidal-dl-input.txt"
    track_n, album_n = build_tidal_input_file(m, out)
    assert track_n == 1  # only 1 matched track
    assert album_n == 1  # only 1 matched album
    text = out.read_text()
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert "https://tidal.com/browse/track/253822017" in lines
    assert "https://tidal.com/browse/album/58138532" in lines
    # Unmatched track should NOT be in the file
    assert "Unmatched Song" not in text
    # Sections are present
    assert "=== Playlist: My Mix (2 tracks) ===" in text
    assert "=== Artist: The Beatles ===" in text


def test_manifest_handles_missing_optional_fields(tmp_path: Path):
    """Manifest load should tolerate older manifests without newer fields."""
    raw = {
        "version": 1,
        "generated_at": "2024-01-01T00:00:00+00:00",
        "spotify_user_id": "bob",
        "playlists": [
            {
                "spotify_id": "p1",
                "name": "Empty",
                "owner": "bob",
                "description": "",
                "public": True,
                "collaborative": False,
                "track_count": 0,
                "tracks": [],
            }
        ],
        "artists": [],
    }
    out = tmp_path / "m.json"
    out.write_text(json.dumps(raw))
    m = Manifest.load_json(out)
    assert m.playlists[0].name == "Empty"
    assert m.playlists[0].tracks == []
    assert m.stats == {}  # not computed yet
    m.compute_stats()
    assert m.stats["playlists"] == 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
