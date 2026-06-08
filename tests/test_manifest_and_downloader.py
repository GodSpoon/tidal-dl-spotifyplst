"""Tests for manifest serialization, tidal-dl input file generation, and the
429-retry wrapper around tiddl in the downloader."""
from __future__ import annotations

import json
import subprocess
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
from spotify_to_tidal.downloader import (  # noqa: E402
    build_tidal_input_file,
    run_tidal_dl,
)


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


def test_qobuz_fields_roundtrip(tmp_path: Path):
    """Qobuz fields on TrackEntry and AlbumEntry survive a JSON round-trip."""
    import json as _json
    track = TrackEntry(
        spotify_id="t_q", spotify_uri="spotify:track:t_q",
        name="Clair de lune", duration_ms=300000,
        artists=["Debussy"], album="Pour le piano", album_id="al_q",
        isrc="FRABX0000001", explicit=False,
        qobuz_id="12345678",
        qobuz_title="Clair de lune",
        qobuz_artist="Debussy",
        qobuz_album="Pour le piano",
        qobuz_album_id="98765",
        qobuz_duration=301,
        matched=True,
    )
    d = _json.loads(_json.dumps(track.__dict__))
    # Simulate round-trip through _track_from_dict by saving/loading a manifest
    m = new_manifest("test_user")
    pl = PlaylistEntry(
        spotify_id="pl_q", name="Qobuz Pl", owner="test_user",
        description="", public=False, collaborative=False,
        track_count=1, tracks=[track],
    )
    m.playlists.append(pl)
    out = tmp_path / "qobuz_rt.json"
    m.save_json(out)
    loaded = Manifest.load_json(out)
    lt = loaded.playlists[0].tracks[0]
    assert lt.qobuz_id == "12345678"
    assert lt.qobuz_title == "Clair de lune"
    assert lt.qobuz_artist == "Debussy"
    assert lt.qobuz_album == "Pour le piano"
    assert lt.qobuz_album_id == "98765"
    assert lt.qobuz_duration == 301


def test_qobuz_album_fields_roundtrip(tmp_path: Path):
    """Qobuz fields on AlbumEntry survive a JSON round-trip."""
    album = AlbumEntry(
        spotify_id="al_q", spotify_uri="spotify:album:al_q",
        name="Pour le piano", artists=["Debussy"],
        album_type="album", total_tracks=3, release_date="1901-01-01",
        qobuz_id="qal_001",
        qobuz_title="Pour le piano",
        qobuz_artist="Claude Debussy",
        qobuz_release_date="1901-01-01",
        qobuz_num_tracks=3,
        matched=True,
    )
    m = new_manifest("test_user2")
    from spotify_to_tidal.manifest import ArtistEntry
    ar = ArtistEntry(
        spotify_id="ar_q", name="Debussy",
        genres=["classical"], popularity=70, followers=500_000,
        albums=[album],
    )
    m.artists.append(ar)
    out = tmp_path / "qobuz_al_rt.json"
    m.save_json(out)
    loaded = Manifest.load_json(out)
    la = loaded.artists[0].albums[0]
    assert la.qobuz_id == "qal_001"
    assert la.qobuz_title == "Pour le piano"
    assert la.qobuz_artist == "Claude Debussy"
    assert la.qobuz_release_date == "1901-01-01"
    assert la.qobuz_num_tracks == 3


def test_qobuz_fields_default_none():
    """New Qobuz fields default to None on existing construction patterns."""
    t = TrackEntry(
        spotify_id="t_x", spotify_uri="spotify:track:t_x",
        name="Song", duration_ms=180000,
        artists=["Artist"], album="Album", album_id="al_x",
        isrc="", explicit=False,
    )
    assert t.qobuz_id is None
    assert t.qobuz_title is None
    assert t.qobuz_album_id is None
    assert t.qobuz_duration is None
    al = AlbumEntry(
        spotify_id="al_x", spotify_uri="spotify:album:al_x",
        name="Album", artists=["Artist"],
        album_type="album", total_tracks=10, release_date="2020-01-01",
    )
    assert al.qobuz_id is None
    assert al.qobuz_num_tracks is None

def _write_input(tmp_path: Path, urls: list[str]) -> Path:
    p = tmp_path / "urls.txt"
    p.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return p


class _FakeResult:
    def __init__(self, rc: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_run_tidal_dl_retries_on_429_then_succeeds(tmp_path, monkeypatch):
    """A chunk that 429s on the first attempt and succeeds on the second
    must trigger exactly one retry and return 0 overall."""
    inp = _write_input(tmp_path, [
        "https://tidal.com/track/1",
        "https://tidal.com/track/2",
    ])
    calls: list[dict] = []

    def fake_run(cmd, check=False, capture_output=False, text=False, timeout=None):  # noqa: ARG001
        calls.append({"capture_output": capture_output})
        if len(calls) == 1:
            return _FakeResult(1)  # streamed first attempt: non-zero
        if len(calls) == 2:
            # captured diagnostic run: shows a 429 line
            return _FakeResult(
                1,
                stdout="API Error: Response body does not contain valid json., 429/0 (track/1)\n",
            )
        return _FakeResult(0)  # retry succeeds
    sleeps: list[float] = []
    monkeypatch.setattr("spotify_to_tidal.downloader.subprocess.run", fake_run)
    rc = run_tidal_dl(
        inp,
        output_dir=tmp_path / "out",
        chunk_size=10,
        max_429_retries=3,
        sleep_fn=sleeps.append,
    )
    assert rc == 0
    assert len(calls) == 3, f"expected 1 streamed + 1 captured + 1 retried, got {calls}"
    # first run streamed (no capture), diagnostic run captured, retry streamed
    assert calls[0]["capture_output"] is False
    assert calls[1]["capture_output"] is True
    assert calls[2]["capture_output"] is False
    # one backoff sleep, ~2s
    assert sleeps == [2]


def test_run_tidal_dl_does_not_retry_non_429_error(tmp_path, monkeypatch):
    """A chunk that errors out for a non-429 reason (e.g. tiddl crash) must
    NOT trigger a retry — we only recover from rate-limits."""
    inp = _write_input(tmp_path, ["https://tidal.com/track/1"])
    calls: list[dict] = []
    def fake_run(cmd, check=False, capture_output=False, text=False, timeout=None):  # noqa: ARG001
        calls.append({"capture_output": capture_output})
        if len(calls) == 1:
            return _FakeResult(2)  # streamed
        if len(calls) == 2:
            return _FakeResult(2, stderr="Traceback ... ValueError: nope\n")
        return _FakeResult(0)  # unreachable
    sleeps: list[float] = []
    monkeypatch.setattr("spotify_to_tidal.downloader.subprocess.run", fake_run)
    rc = run_tidal_dl(
        inp,
        output_dir=tmp_path / "out",
        chunk_size=10,
        max_429_retries=3,
        sleep_fn=sleeps.append,
    )
    assert rc == 2
    assert len(calls) == 2  # streamed + captured diagnostic, then gave up
    assert sleeps == []  # no backoff


def test_run_tidal_dl_gives_up_after_max_429_retries(tmp_path, monkeypatch):
    """If a chunk keeps 429ing past max_429_retries, we stop retrying and
    return tiddl's last rc."""
    inp = _write_input(tmp_path, ["https://tidal.com/track/1"])
    calls: list[dict] = []
    sleep_count = 0
    def fake_run(cmd, check=False, capture_output=False, text=False, timeout=None):  # noqa: ARG001
        nonlocal sleep_count
        calls.append({"capture_output": capture_output})
        if capture_output:
            sleep_count += 1
            return _FakeResult(
                1,
                stdout="API Error: ..., 429/0 (track/1)\n",
            )
        return _FakeResult(1)  # streamed attempt: still 429'ing
    monkeypatch.setattr("spotify_to_tidal.downloader.subprocess.run", fake_run)
    rc = run_tidal_dl(
        inp,
        output_dir=tmp_path / "out",
        chunk_size=10,
        max_429_retries=2,
        sleep_fn=lambda _s: None,
    )
    assert rc == 1
    # streamed, captured, streamed, captured, streamed = 5 invocations
    # before the 2nd retry's check (attempt >= max) trips
    assert len(calls) == 5


def test_run_tidal_dl_times_out_on_hung_chunk(tmp_path, monkeypatch):
    """If tiddl hangs longer than chunk_timeout, we kill it and move on."""
    inp = _write_input(tmp_path, ["https://tidal.com/track/1"])
    calls: list[dict] = []

    def fake_run(cmd, check=False, capture_output=False, text=False, timeout=None):  # noqa: ARG001
        calls.append({"capture_output": capture_output, "timeout": timeout})
        raise subprocess.TimeoutExpired(cmd="tiddl", timeout=timeout)

    monkeypatch.setattr("spotify_to_tidal.downloader.subprocess.run", fake_run)
    rc = run_tidal_dl(
        inp,
        output_dir=tmp_path / "out",
        chunk_size=10,
        max_429_retries=0,
        chunk_timeout=5.0,
        sleep_fn=lambda _s: None,
    )
    assert rc == 1
    assert len(calls) == 1
    assert calls[0]["timeout"] == 5.0

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
