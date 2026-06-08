"""Tests for the squidwtf headless downloader backend."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from spotify_to_tidal.downloaders.factory import get_downloader, list_backends  # noqa: E402
from spotify_to_tidal.downloaders.squidwtf import (  # noqa: E402
    _build_download_plan,
    _download_track,
    _safe_name,
    _stream_to_disk,
)
from spotify_to_tidal.manifest import (  # noqa: E402
    AlbumEntry,
    ArtistEntry,
    Manifest,
    PlaylistEntry,
    TrackEntry,
    new_manifest,
)


def test_squidwtf_registered_in_factory():
    assert "squidwtf" in list_backends()
    backend = get_downloader("squidwtf")
    assert backend.name == "squidwtf"


def test_safe_name():
    assert _safe_name("Kate Bush") == "Kate Bush"
    assert _safe_name("AC/DC") == "AC_DC"
    assert _safe_name("  hello world  ") == "hello world"
    assert _safe_name("Album: Special Edition!") == "Album_ Special Edition"


def _sample_manifest() -> Manifest:
    m = new_manifest("alice")

    pl = PlaylistEntry(
        spotify_id="pl1",
        name="Test Playlist",
        owner="alice",
        description="",
        public=False,
        collaborative=False,
        track_count=3,
        tracks=[
            TrackEntry(
                spotify_id="t1",
                spotify_uri="spotify:track:t1",
                name="Track One",
                duration_ms=180_000,
                artists=["Artist A"],
                album="Album X",
                album_id="alx1",
                isrc="ISRC001",
                explicit=False,
                matched=True,
                qobuz_id="12345",
                qobuz_album_id="alb001",
            ),
            TrackEntry(
                spotify_id="t2",
                spotify_uri="spotify:track:t2",
                name="Track Two",
                duration_ms=200_000,
                artists=["Artist A"],
                album="Album X",
                album_id="alx1",
                isrc="ISRC002",
                explicit=False,
                matched=True,
                qobuz_id="12346",
                qobuz_album_id="alb001",
            ),
            TrackEntry(
                spotify_id="t3",
                spotify_uri="spotify:track:t3",
                name="Track Three",
                duration_ms=210_000,
                artists=["Artist B"],
                album="Album Y",
                album_id="aly1",
                isrc="ISRC003",
                explicit=False,
                matched=True,
                qobuz_id="99999",
            ),
        ],
    )
    m.playlists.append(pl)

    artist = ArtistEntry(
        spotify_id="ar1",
        name="The Beatles",
        genres=["rock"],
        popularity=85,
        followers=10_000_000,
        matched=True,
        albums=[
            AlbumEntry(
                spotify_id="al1",
                spotify_uri="spotify:album:al1",
                name="Abbey Road",
                artists=["The Beatles"],
                album_type="album",
                total_tracks=17,
                release_date="1969-09-26",
                matched=True,
                qobuz_id="abbey123",
            ),
        ],
    )
    m.artists.append(artist)

    return m


def test_build_download_plan_groups_playlist_albums():
    """Two tracks from the same album in a playlist should collapse to one album task."""
    m = _sample_manifest()
    plan = _build_download_plan(m)

    album_tasks = [t for t in plan if t["type"] == "album"]
    track_tasks = [t for t in plan if t["type"] == "track"]

    # alb001 (2 tracks) + Abbey Road (artist album) = 2 album tasks
    assert len(album_tasks) == 2
    # Track Three is alone → 1 track task
    assert len(track_tasks) == 1
    assert track_tasks[0]["track_id"] == 99999

def test_build_download_plan_skips_unmatched():
    m = _sample_manifest()
    # Mark every track in alb001 as unmatched — then the album task should disappear
    for t in m.playlists[0].tracks:
        if t.qobuz_album_id == "alb001":
            t.matched = False
    plan = _build_download_plan(m)
    ids = {t.get("album_id") or t.get("track_id") for t in plan}
    assert "alb001" not in ids
    assert 99999 in ids


def test_build_download_plan_empty_manifest():
    m = new_manifest("alice")
    assert _build_download_plan(m) == []


def test_stream_to_disk(tmp_path: Path):
    """_stream_to_disk should write chunks to the destination file."""
    dest = tmp_path / "test.flac"
    mock_resp = MagicMock()
    mock_resp.iter_content.return_value = [b"chunk1", b"chunk2"]
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    _stream_to_disk("http://example.com/file", dest, mock_session)

    assert dest.read_bytes() == b"chunk1chunk2"
    mock_session.get.assert_called_once_with("http://example.com/file", stream=True, timeout=120)


def test_download_track_skips_existing_file(tmp_path: Path):
    """If the destination already exists and is non-empty, skip the download."""
    out_dir = tmp_path / "downloads"
    dest = out_dir / "Artist" / "Album" / "01 Title.flac"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("already here", encoding="utf-8")

    task = {
        "type": "track",
        "track_id": 123,
        "artist": "Artist",
        "album": "Album",
        "title": "Title",
        "track_number": 1,
        "source": "test",
    }
    mock_session = MagicMock()
    ok, msg = _download_track(task, "27", out_dir, mock_session)

    assert ok is True
    assert "SKIP" in msg
    mock_session.get.assert_not_called()


def test_download_track_retries_on_429(tmp_path: Path, monkeypatch):
    """A 429 response should trigger retry with backoff, then succeed."""
    out_dir = tmp_path / "downloads"
    task = {
        "type": "track",
        "track_id": 123,
        "artist": "Artist",
        "album": "Album",
        "title": "Title",
        "track_number": 1,
        "source": "test",
    }

    call_count = 0

    def fake_download_url(track_id, quality, session):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise requests.HTTPError("429", response=MagicMock(status_code=429))
        return "http://example.com/file"

    monkeypatch.setattr(
        "spotify_to_tidal.downloaders.squidwtf._download_url", fake_download_url
    )

    sleeps: list[float] = []
    monkeypatch.setattr(
        "spotify_to_tidal.downloaders.squidwtf.time.sleep", sleeps.append
    )

    mock_session = MagicMock()
    with patch("spotify_to_tidal.downloaders.squidwtf._stream_to_disk"):
        ok, msg = _download_track(task, "27", out_dir, mock_session)

    assert ok is True
    assert call_count == 2
    assert sleeps == [2.0]


def test_download_track_fails_after_max_retries(tmp_path: Path, monkeypatch):
    """If every attempt 429s, we should give up after MAX_RETRIES."""
    out_dir = tmp_path / "downloads"
    task = {
        "type": "track",
        "track_id": 123,
        "artist": "Artist",
        "album": "Album",
        "title": "Title",
        "track_number": 1,
        "source": "test",
    }

    call_count = 0

    def fake_download_url(track_id, quality, session):
        nonlocal call_count
        call_count += 1
        raise requests.HTTPError("429", response=MagicMock(status_code=429))

    monkeypatch.setattr(
        "spotify_to_tidal.downloaders.squidwtf._download_url", fake_download_url
    )

    sleeps: list[float] = []
    monkeypatch.setattr(
        "spotify_to_tidal.downloaders.squidwtf.time.sleep", sleeps.append
    )

    mock_session = MagicMock()
    ok, msg = _download_track(task, "27", out_dir, mock_session)

    assert ok is False
    assert call_count == 4  # _MAX_RETRIES
    assert len(sleeps) == 3  # sleep between attempts, not after last


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
