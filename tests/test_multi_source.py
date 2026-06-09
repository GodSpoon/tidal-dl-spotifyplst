"""Tests for multi-source manifest, downloader factory, and parallel runner."""
from __future__ import annotations

from pathlib import Path

import pytest

from spotify_to_tidal.downloaders import DownloaderBackend, run_backends_in_parallel
from spotify_to_tidal.downloaders.factory import get_downloader_names
from spotify_to_tidal.downloaders.qobuz import QobuzDownloader, _build_qobuz_url_list
from spotify_to_tidal.manifest import AlbumEntry, ArtistEntry, Manifest, PlaylistEntry, TrackEntry


class FakeBackend(DownloaderBackend):
    def __init__(self, name: str, rc: int = 0):
        self._name = name
        self._rc = rc

    @property
    def name(self) -> str:
        return self._name

    def download(self, manifest, cfg, **kwargs) -> int:
        return self._rc


def test_get_downloader_names():
    assert get_downloader_names("all") == ["tiddl", "qobuz", "squidwtf"]
    assert get_downloader_names("squidwtf") == ["squidwtf"]
    assert get_downloader_names("tiddl") == ["tiddl"]
    assert get_downloader_names("unknown") == ["unknown"]


def test_run_backends_in_parallel_empty():
    from spotify_to_tidal.config import AppConfig

    cfg = AppConfig(
        spotify_client_id="x",
        spotify_client_secret="y",
        output_dir=Path("/tmp"),
        tidal_download_dir=Path("/tmp"),
    )
    manifest = Manifest(version=1, spotify_user_id="u", generated_at="")
    assert run_backends_in_parallel(manifest, cfg, []) == {}
def test_run_backends_in_parallel_with_fakes(tmp_path):
    from spotify_to_tidal.config import AppConfig

    cfg = AppConfig(
        spotify_client_id="x",
        spotify_client_secret="y",
        output_dir=tmp_path,
        tidal_download_dir=tmp_path,
    )
    manifest = Manifest(version=1, spotify_user_id="u", generated_at="")

    import spotify_to_tidal.downloaders as _dl_mod

    original_factory = getattr(_dl_mod, "factory", None)
    fake_factory = type("F", (), {"get_downloader": lambda self, n: FakeBackend(n, rc=0)})()
    _dl_mod.factory = fake_factory  # type: ignore[attr-defined]
    try:
        rcs = run_backends_in_parallel(manifest, cfg, ["a", "b"])
        assert rcs == {"a": 0, "b": 0}
    finally:
        if original_factory is not None:
            _dl_mod.factory = original_factory  # type: ignore[attr-defined]


def test_run_backends_in_parallel_catches_exceptions(tmp_path):
    from spotify_to_tidal.config import AppConfig

    cfg = AppConfig(
        spotify_client_id="x",
        spotify_client_secret="y",
        output_dir=tmp_path,
        tidal_download_dir=tmp_path,
    )
    manifest = Manifest(version=1, spotify_user_id="u", generated_at="")

    class ExplodingBackend(DownloaderBackend):
        @property
        def name(self) -> str:
            return "boom"

        def download(self, manifest, cfg, **kwargs) -> int:
            raise RuntimeError("kaboom")

    import spotify_to_tidal.downloaders as _dl_mod

    original_factory = getattr(_dl_mod, "factory", None)
    fake_factory = type("F", (), {"get_downloader": lambda self, n: ExplodingBackend()})()
    _dl_mod.factory = fake_factory  # type: ignore[attr-defined]
    try:
        rcs = run_backends_in_parallel(manifest, cfg, ["boom"])
        assert rcs == {"boom": 1}
    finally:
        if original_factory is not None:
            _dl_mod.factory = original_factory  # type: ignore[attr-defined]


def test_qobuz_fields_roundtrip(tmp_path):
    t = TrackEntry(
        spotify_id="s1",
        spotify_uri="spotify:track:s1",
        name="Song",
        duration_ms=200_000,
        artists=["A"],
        album="Album",
        album_id="a1",
        isrc="USXXX",
        explicit=False,
        qobuz_id="q123",
        qobuz_title="Q Song",
        qobuz_album="Q Album",
    )
    m = Manifest(version=1, spotify_user_id="u", generated_at="now", playlists=[
        PlaylistEntry(
            spotify_id="p1", name="P", owner="o", description="", public=True,
            collaborative=False, track_count=1, tracks=[t],
        )
    ])
    path = tmp_path / "m.json"
    m.save_json(path)
    loaded = Manifest.load_json(path)
    lt = loaded.playlists[0].tracks[0]
    assert lt.qobuz_id == "q123"
    assert lt.qobuz_title == "Q Song"
    assert lt.qobuz_album == "Q Album"
    assert lt.matched is False


def test_qobuz_url_list_groups_albums():
    t1 = TrackEntry(
        spotify_id="s1", spotify_uri="u1", name="A", duration_ms=100_000,
        artists=["X"], album="Album", album_id="a1", isrc="I1", explicit=False,
        qobuz_id="qt1", qobuz_album_id="qa1", matched=True,
    )
    t2 = TrackEntry(
        spotify_id="s2", spotify_uri="u2", name="B", duration_ms=100_000,
        artists=["X"], album="Album", album_id="a1", isrc="I2", explicit=False,
        qobuz_id="qt2", qobuz_album_id="qa1", matched=True,
    )
    t3 = TrackEntry(
        spotify_id="s3", spotify_uri="u3", name="C", duration_ms=100_000,
        artists=["X"], album="Other", album_id="a2", isrc="I3", explicit=False,
        qobuz_id="qt3", qobuz_album_id="qa2", matched=True,
    )
    m = Manifest(
        version=1, spotify_user_id="u", generated_at="now",
        playlists=[
            PlaylistEntry(
                spotify_id="p1", name="P", owner="o", description="",
                public=True, collaborative=False, track_count=3, tracks=[t1, t2, t3],
            )
        ],
    )
    urls = _build_qobuz_url_list(m)
    assert len(urls) == 2
    assert ("https://play.qobuz.com/album/qa1", "album") in urls
    assert ("https://play.qobuz.com/track/qt3", "track") in urls


def test_qobuz_downloader_name():
    assert QobuzDownloader().name == "qobuz"


def test_qobuz_album_fields_roundtrip(tmp_path):
    al = AlbumEntry(
        spotify_id="sa1", spotify_uri="ua1", name="Album",
        artists=["A"], album_type="album", total_tracks=10, release_date="2020-01-01",
        qobuz_id="qa1", qobuz_title="Q Album", qobuz_artist="Q Artist",
        qobuz_release_date="2020-01-01", qobuz_num_tracks=10,
    )
    m = Manifest(
        version=1, spotify_user_id="u", generated_at="now",
        artists=[ArtistEntry(
            spotify_id="ar1", name="A", genres=[], popularity=0, followers=0,
            albums=[al],
        )],
    )
    path = tmp_path / "m.json"
    m.save_json(path)
    loaded = Manifest.load_json(path)
    la = loaded.artists[0].albums[0]
    assert la.qobuz_id == "qa1"
    assert la.qobuz_title == "Q Album"
    assert la.qobuz_num_tracks == 10
