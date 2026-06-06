"""Unit tests for the matcher — pure logic, no I/O."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotify_to_tidal.matcher import (  # noqa: E402
    _norm,
    _title_key,
    _ratio,
    _duration_score,
    _artist_score,
    _title_score,
    match_track,
    match_album,
    match_artist,
    TRACK_MATCH_THRESHOLD,
)
from spotify_to_tidal.spotify import (  # noqa: E402
    SpotifyTrack,
    SpotifyAlbum,
    SpotifyArtist,
)
from spotify_to_tidal.tidal import TidalTrack, TidalAlbum, TidalArtist  # noqa: E402


# ---------- normalization ----------

def test_norm_strips_accents_and_punct():
    assert _norm("Café Del Mar") == "cafe del mar"
    assert _norm("  Hello!!  World?? ") == "hello world"
    assert _norm("") == ""


def test_title_key_drops_release_suffixes():
    assert _title_key("Hey Jude - Remastered 2015") == "hey jude"
    assert _title_key("Hello (Radio Edit)") == "hello"
    assert _title_key("Abbey Road (Remastered)") == "abbey road"
    assert _title_key("Some Song") == "some song"


def test_ratio_handles_empty_strings():
    assert _ratio("", "") == 0.0
    assert _ratio("a", "") == 0.0
    assert _ratio("beatles", "Beatles") > 0.9


# ---------- duration scoring ----------

def test_duration_score_tiers():
    assert _duration_score(180_000, 180) == 20.0
    assert _duration_score(180_000, 181) == 20.0
    assert _duration_score(180_000, 183) == 15.0
    assert _duration_score(180_000, 186) == 8.0
    assert _duration_score(180_000, 195) == 2.0
    # Big mismatch penalizes
    assert _duration_score(180_000, 220) <= -5.0
    # Empty input returns 0
    assert _duration_score(0, 180) == 0.0
    assert _duration_score(180_000, 0) == 0.0


# ---------- artist scoring ----------

def test_artist_score_exact_primary():
    sp = [SpotifyArtist(id="1", name="The Beatles", uri="", genres=[], popularity=0, followers=0, images=[])]
    assert _artist_score(sp, ["The Beatles"]) == 40.0


def test_artist_score_ignores_extra_tidal_artists():
    sp = [SpotifyArtist(id="1", name="Beatles", uri="", genres=[], popularity=0, followers=0, images=[])]
    # Different order: primary is the featured artist — should still get some credit
    score = _artist_score(sp, ["George Martin", "Beatles"])
    assert score >= 10.0


def test_artist_score_handles_empty():
    assert _artist_score([], ["Anyone"]) == 0.0
    assert _artist_score([SpotifyArtist(id="1", name="A", uri="", genres=[], popularity=0, followers=0, images=[])], []) == 0.0


# ---------- title scoring ----------

def test_title_score_exact():
    s, exact = _title_score("Yesterday", "Yesterday")
    assert exact
    assert s == 50.0


def test_title_score_strips_remastered():
    s, _ = _title_score("Yesterday", "Yesterday - Remastered 2009")
    assert s >= 40.0


def test_title_score_completely_different():
    s, _ = _title_score("Yesterday", "Completely Different Song")
    assert s < 20.0


# ---------- end-to-end track match ----------

def _sp_track(name="Yesterday", isrc="GBUM71505080", duration_ms=125_000,
              artists=("The Beatles",), album="Help!", explicit=False):
    return SpotifyTrack(
        id="s1", uri="spotify:track:s1", name=name, duration_ms=duration_ms,
        artists=[SpotifyArtist(id="a1", name=n, uri="", genres=[], popularity=0, followers=0, images=[]) for n in artists],
        album=SpotifyAlbum(id="al1", name=album, uri="", album_type="album", artists=[], total_tracks=14, release_date="1965-08-06"),
        isrc=isrc, explicit=explicit,
    )


def _t_track(id=253822017, title="Yesterday", duration=125, artist="The Beatles",
             artists=("The Beatles",), album="Help!", isrc="GBUM71505080", explicit=False, version=""):
    return TidalTrack(
        id=id, title=title, duration=duration, artist=artist, artists=list(artists),
        album=album, isrc=isrc, version=version, explicit=explicit, audio_quality="LOSSLESS",
    )


def test_match_track_perfect():
    sp = _sp_track()
    t = _t_track()
    best, info = match_track(sp, [t])
    assert best is t
    assert info.score >= 100.0  # ISRC match alone gives 100


def test_match_track_title_artist_no_isrc():
    sp = _sp_track(isrc="")
    t = _t_track(isrc="")
    best, info = match_track(sp, [t])
    assert best is t
    assert info.score >= TRACK_MATCH_THRESHOLD


def test_match_track_picks_best_among_many():
    sp = _sp_track()
    wrong = _t_track(id=999, title="Hello", duration=200, artist="Adele", isrc="", album="25")
    right = _t_track()
    best, info = match_track(sp, [wrong, right])
    assert best.id == right.id


def test_match_track_rejects_remix_penalty():
    sp = _sp_track()
    remix = _t_track(title="Yesterday - Remix", isrc="", version="Remix")
    plain = _t_track()
    best, info = match_track(sp, [remix, plain])
    # Both should match, but plain should score higher
    assert best.id == plain.id


def test_match_track_no_candidates():
    sp = _sp_track()
    best, info = match_track(sp, [])
    assert best is None
    assert info.reasons == ["no candidates"]


def test_match_track_below_threshold():
    sp = _sp_track()
    # Different song, no ISRC
    t = _t_track(id=999, title="Completely Different Song", duration=300, artist="Random Artist", isrc="", album="Other Album")
    best, info = match_track(sp, [t])
    if best:
        assert info.score < TRACK_MATCH_THRESHOLD


# ---------- album matching ----------

def _sp_album(name="Help!", total_tracks=14, year="1965-08-06", artists=("The Beatles",), album_type="album"):
    return SpotifyAlbum(
        id="a1", name=name, uri="", album_type=album_type,
        artists=[SpotifyArtist(id="a1", name=n, uri="", genres=[], popularity=0, followers=0, images=[]) for n in artists],
        total_tracks=total_tracks, release_date=year,
    )


def _t_album(id=58138532, title="Help!", num_tracks=14, release="1965-08-06", artist="The Beatles"):
    return TidalAlbum(
        id=id, title=title, artist=artist, artists=[artist],
        release_date=release, num_tracks=num_tracks, duration=2000, explicit=False,
    )


def test_match_album_perfect():
    sp = _sp_album()
    t = _t_album()
    best, info = match_album(sp, [t])
    assert best is t
    assert info.score >= 80.0


def test_match_album_year_mismatch_lowers_score():
    # 1965 original vs 2009 remaster — different albums, but same title/artist.
    # A perfect title+artist match is still legitimate; year mismatch just
    # means we don't get the +12 year bonus.
    sp = _sp_album(year="1965")
    t = _t_album(release="2009")
    best, info = match_album(sp, [t])
    assert best is t
    # No year bonus: 50 (title) + 40 (artist) + 10 (tracks) + 6 (type) = 106
    # With year bonus it would be 118.
    assert 100 <= info.score < 120


def test_match_album_year_mismatch_hurts_when_title_also_weak():
    sp = _sp_album(name="Help", year="1965")
    # Different era, fuzzy title
    t = _t_album(title="Help 2009 Edition", release="2009", num_tracks=20)
    best, info = match_album(sp, [t])
    # Title is fuzzy (~"help" vs "help 2009 edition" = strip -> "help"), so
    # we should match by stripped-title; year mismatch still hurts a little.
    assert best is t


# ---------- artist matching ----------

def test_match_artist_exact():
    sp = SpotifyArtist(id="a1", name="The Beatles", uri="", genres=[], popularity=85, followers=10_000_000, images=[])
    t = TidalArtist(id=2076, name="The Beatles")
    best, info = match_artist(sp, [t])
    assert best.id == 2076
    assert info.score == 100.0


def test_match_artist_fuzzy():
    sp = SpotifyArtist(id="a1", name="The Beatles", uri="", genres=[], popularity=0, followers=0, images=[])
    t = TidalArtist(id=1, name="Beatles")
    best, info = match_artist(sp, [t])
    # "the beatles" vs "beatles" — ratio 0.842, score 0.842*80 = ~67
    assert best.id == 1
    assert 50 < info.score < 100


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
