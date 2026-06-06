"""Score Tidal search results against Spotify metadata.

The hard truth: Spotify and Tidal don't share catalog IDs. The only
reliable join keys are:

  * ISRC                 — best for tracks
  * UPC                  — best for albums (not in Tidal search responses)
  * title + artist       — fallback
  * duration / track count — disambiguator

This module implements a scored, deterministic matcher with a tunable
threshold. Higher score = better match.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from .spotify import SpotifyTrack, SpotifyAlbum, SpotifyArtist
from .tidal import TidalTrack, TidalAlbum, TidalArtist

# ---------- string normalization ----------

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")
# Suffixes that vary across platforms, optionally followed by a year/version.
_SUFFIX_TOKENS = (
    "remastered",
    "remastered version",
    "deluxe edition",
    "deluxe",
    "deluxe version",
    "bonus track version",
    "explicit version",
    "clean version",
    "radio edit",
    "single version",
    "album version",
    "original mix",
    "anniversary edition",
    "expanded edition",
    "remix",
    "edit",
    "mix",
    "version",
    "live",
    "karaoke",
    "instrumental",
)
# We run the regex against the *raw* (pre-normalized) string so we can see
# brackets and dashes. Then we normalize the remainder.
_BRACKETED_SUFFIX_RE = re.compile(
    r"\s*[\(\[\-]\s*(?P<word>"
    + "|".join(re.escape(w) for w in _SUFFIX_TOKENS)
    + r")\b[^)\]]*$",
    re.IGNORECASE,
)
# Bare suffix at end of an already-normalized string (no brackets left).
_BARE_SUFFIX_RE = re.compile(
    r"\s+(?P<word>" + "|".join(re.escape(w) for w in _SUFFIX_TOKENS) + r")\b.*$",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def _title_key(s: str) -> str:
    """Drop common release suffixes that vary between platforms.

    Handles both bracketed ones ("Yesterday - Remastered 2009",
    "Help! (Deluxe Edition)") and bare ones ("Hey Jude Remastered").
    """
    # Strip bracketed suffixes first, against the raw input, so we can see
    # the separator characters.
    for _ in range(3):
        new = _BRACKETED_SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    s = _norm(s)
    # Then strip any bare trailing suffix.
    for _ in range(3):
        new = _BARE_SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


def _ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------- scoring ----------

@dataclass
class Match:
    score: float
    reasons: list[str]


def _isrc_match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.strip().upper() == b.strip().upper()


def _duration_score(spotify_ms: int, tidal_seconds: int) -> float:
    if not spotify_ms or not tidal_seconds:
        return 0.0
    delta = abs(spotify_ms / 1000.0 - tidal_seconds)
    if delta <= 1.5:
        return 20.0
    if delta <= 3.0:
        return 15.0
    if delta <= 6.0:
        return 8.0
    if delta <= 15.0:
        return 2.0
    return -10.0  # actively penalize big mismatches


def _title_score(a: str, b: str) -> tuple[float, bool]:
    a, b = _title_key(a), _title_key(b)
    if not a or not b:
        return 0.0, False
    if a == b:
        return 50.0, True
    r = SequenceMatcher(None, a, b).ratio()
    return r * 35.0, False  # up to 35 for fuzzy title


def _artist_score(spotify_artists, tidal_artist_names: list[str]) -> float:
    if not spotify_artists or not tidal_artist_names:
        return 0.0
    s_names = [_norm(a.name if hasattr(a, "name") else a) for a in spotify_artists]
    t_names = [_norm(n) for n in tidal_artist_names if n]
    if not t_names:
        return 0.0
    # Best match: any spotify name equals the primary tidal artist name
    primary = t_names[0]
    if primary in s_names:
        return 40.0
    # Fuzzy match the primary
    best = 0.0
    for s in s_names:
        best = max(best, _ratio(s, primary) * 30.0)
    # Partial credit for any featured artist overlap
    for s in s_names:
        for t in t_names[1:]:
            if s == t:
                best = max(best, 12.0)
    return best


def _track_count_score(spotify_total: int, tidal_total: int) -> float:
    if not spotify_total or not tidal_total:
        return 0.0
    if spotify_total == tidal_total:
        return 10.0
    if abs(spotify_total - tidal_total) <= 2:
        return 5.0
    return -5.0


# ---------- public API ----------

def match_track(spotify: SpotifyTrack, tidal_candidates: list[TidalTrack]) -> tuple[Optional[TidalTrack], Match]:
    if not tidal_candidates:
        return None, Match(0.0, ["no candidates"])

    best: Optional[TidalTrack] = None
    best_score = -1e9
    best_reasons: list[str] = []

    for t in tidal_candidates:
        score = 0.0
        reasons: list[str] = []
        if _isrc_match(spotify.isrc, t.isrc):
            score += 100.0
            reasons.append(f"isrc={t.isrc}")
        ts, exact_title = _title_score(spotify.name, t.title)
        score += ts
        if ts:
            reasons.append(f"title={ts:.0f}" + ("*" if exact_title else ""))
        a_score = _artist_score(spotify.artists, t.artists or [t.artist])
        score += a_score
        reasons.append(f"artist={a_score:.0f}")
        score += _duration_score(spotify.duration_ms, t.duration)
        if t.album and spotify.album:
            album_ratio = _ratio(spotify.album.name, t.album)
            if album_ratio >= 0.95:
                score += 8.0
                reasons.append("album+8")
            elif album_ratio >= 0.7:
                score += 3.0
        # Penalize remixes/edits/versions when not in the title
        version_words = ("remix", "edit", "mix", "version", "live", "remaster", "karaoke", "instrumental")
        s_norm = _norm(spotify.name)
        t_norm = _norm(t.title)
        for w in version_words:
            if w in t_norm and w not in s_norm:
                score -= 12.0
                reasons.append(f"{w}-12")
        if t.explicit and not spotify.explicit:
            score -= 4.0
        if score > best_score:
            best_score = score
            best = t
            best_reasons = reasons

    return best, Match(best_score, best_reasons)


def match_album(spotify: SpotifyAlbum, tidal_candidates: list[TidalAlbum]) -> tuple[Optional[TidalAlbum], Match]:
    if not tidal_candidates:
        return None, Match(0.0, ["no candidates"])

    best: Optional[TidalAlbum] = None
    best_score = -1e9
    best_reasons: list[str] = []

    s_year = (spotify.release_date or "")[:4]

    for a in tidal_candidates:
        score = 0.0
        reasons: list[str] = []
        # Title
        ts, exact = _title_score(spotify.name, a.title)
        score += ts
        reasons.append(f"title={ts:.0f}" + ("*" if exact else ""))
        # Artist
        score += _artist_score(spotify.artists, a.artists or [a.artist])
        # Year
        t_year = (a.release_date or "")[:4]
        if s_year and t_year and s_year == t_year:
            score += 12.0
            reasons.append("year+12")
        elif s_year and t_year and abs(int(s_year) - int(t_year)) <= 1:
            score += 4.0
        # Track count
        score += _track_count_score(spotify.total_tracks, a.num_tracks)
        # Album type signal
        sp_type = spotify.album_type  # album/single/compilation
        t_track_count = a.num_tracks
        if sp_type == "single" and t_track_count <= 3:
            score += 6.0
        elif sp_type == "album" and t_track_count >= 5:
            score += 6.0
        elif sp_type == "compilation" and t_track_count >= 8:
            score += 4.0
        if score > best_score:
            best_score = score
            best = a
            best_reasons = reasons

    return best, Match(best_score, best_reasons)


def match_artist(spotify: SpotifyArtist, tidal_candidates: list[TidalArtist]) -> tuple[Optional[TidalArtist], Match]:
    if not tidal_candidates:
        return None, Match(0.0, ["no candidates"])

    best: Optional[TidalArtist] = None
    best_score = -1e9
    best_reasons: list[str] = []
    s_name = _norm(spotify.name)
    for a in tidal_candidates:
        t_name = _norm(a.name)
        if s_name == t_name:
            score, reasons = 100.0, ["name=100*"]
        else:
            r = _ratio(s_name, t_name)
            score = r * 80.0
            reasons = [f"name={score:.0f}"]
        if score > best_score:
            best_score = score
            best = a
            best_reasons = reasons

    return best, Match(best_score, best_reasons)


# Tunable thresholds
TRACK_MATCH_THRESHOLD = 55.0
ALBUM_MATCH_THRESHOLD = 60.0
ARTIST_MATCH_THRESHOLD = 80.0
