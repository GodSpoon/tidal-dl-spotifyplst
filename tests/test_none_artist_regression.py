"""Regression: TrackEntry/artists must survive Spotify's occasional null name."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spotify_to_tidal.manifest import track_from_spotify, album_from_spotify, new_manifest, PlaylistEntry
from spotify_to_tidal.spotify import SpotifyArtist, SpotifyAlbum, SpotifyTrack


def test_track_with_none_artist_name():
    t = SpotifyTrack(id="1", name="x", uri="spotify:track:1", duration_ms=1000,
                     artists=[SpotifyArtist(id="a", name=None, uri="", genres=[], popularity=0, followers=0, images=[])])
    assert track_from_spotify(t).artists == []


def test_track_with_mixed_artist_names():
    t = SpotifyTrack(id="1", name="x", uri="spotify:track:1", duration_ms=1000,
                     artists=[SpotifyArtist(id="a", name="A", uri="", genres=[], popularity=0, followers=0, images=[]),
                              SpotifyArtist(id="b", name=None, uri="", genres=[], popularity=0, followers=0, images=[])])
    assert track_from_spotify(t).artists == ["A"]


def test_album_with_none_artist_name():
    a = SpotifyAlbum(id="1", name="x", uri="spotify:album:1", album_type="album",
                     artists=[SpotifyArtist(id="a", name=None, uri="", genres=[], popularity=0, followers=0, images=[])])
    assert album_from_spotify(a).artists == []


def test_csv_export_survives_none_artist(tmp_path: Path):
    t = SpotifyTrack(id="1", name="x", uri="spotify:track:1", duration_ms=1000,
                     artists=[SpotifyArtist(id="a", name=None, uri="", genres=[], popularity=0, followers=0, images=[])])
    m = new_manifest("u")
    m.playlists.append(PlaylistEntry(spotify_id="p", name="p", owner="o", description="",
                                      public=False, collaborative=False, track_count=1,
                                      tracks=[track_from_spotify(t)]))
    out = tmp_path / "out.csv"
    m.save_csv(out)
    assert out.stat().st_size > 0
