"""Manifest format for Spotify → Tidal download jobs.

A manifest is a single JSON file with the full Spotify library snapshot
plus (later) the matched Tidal IDs. JSON over CSV because the Spotify
side is deeply nested (track → artists → album) and parsing that into
flat columns is lossy. CSV is supported as an export for human
inspection only.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .spotify import (
    SpotifyPlaylistSummary,
    SpotifyTrack,
    SpotifyAlbum,
    SpotifyArtist,
)

MANIFEST_VERSION = 1


# ---------- serializable containers ----------

@dataclass
class TrackEntry:
    spotify_id: str
    spotify_uri: str
    name: str
    duration_ms: int
    artists: list[str]
    album: str
    album_id: str
    isrc: str
    explicit: bool
    # Filled in by the matcher:
    tidal_id: Optional[int] = None
    tidal_title: Optional[str] = None
    tidal_artist: Optional[str] = None
    tidal_album: Optional[str] = None
    tidal_album_id: Optional[int] = None
    tidal_duration: Optional[int] = None
    match_score: Optional[float] = None
    match_reasons: Optional[list[str]] = None
    matched: bool = False
    error: Optional[str] = None  # e.g., "below threshold", "no candidates"
    # Qobuz match fields:
    qobuz_id: Optional[str] = None
    qobuz_title: Optional[str] = None
    qobuz_artist: Optional[str] = None
    qobuz_album: Optional[str] = None
    qobuz_album_id: Optional[str] = None
    qobuz_duration: Optional[int] = None


@dataclass
class AlbumEntry:
    spotify_id: str
    spotify_uri: str
    name: str
    artists: list[str]
    album_type: str
    total_tracks: int
    release_date: str
    # Filled in by the matcher:
    tidal_id: Optional[int] = None
    tidal_title: Optional[str] = None
    tidal_artist: Optional[str] = None
    tidal_release_date: Optional[str] = None
    tidal_num_tracks: Optional[int] = None
    match_score: Optional[float] = None
    match_reasons: Optional[list[str]] = None
    matched: bool = False
    error: Optional[str] = None
    # Qobuz match fields:
    qobuz_id: Optional[str] = None
    qobuz_title: Optional[str] = None
    qobuz_artist: Optional[str] = None
    qobuz_release_date: Optional[str] = None
    qobuz_num_tracks: Optional[int] = None


@dataclass
class ArtistEntry:
    spotify_id: str
    name: str
    genres: list[str]
    popularity: int
    followers: int
    # Albums collected:
    albums: list[AlbumEntry] = field(default_factory=list)
    # Filled in by the matcher:
    tidal_id: Optional[int] = None
    tidal_name: Optional[str] = None
    match_score: Optional[float] = None
    matched: bool = False
    error: Optional[str] = None


@dataclass
class PlaylistEntry:
    spotify_id: str
    name: str
    owner: str
    description: str
    public: bool
    collaborative: bool
    track_count: int
    tracks: list[TrackEntry] = field(default_factory=list)


@dataclass
class Manifest:
    version: int
    generated_at: str
    spotify_user_id: str
    playlists: list[PlaylistEntry] = field(default_factory=list)
    artists: list[ArtistEntry] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    # ---- to/from dict for JSON ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "spotify_user_id": self.spotify_user_id,
            "playlists": [asdict(p) for p in self.playlists],
            "artists": [asdict(a) for a in self.artists],
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        m = cls(
            version=int(data.get("version", MANIFEST_VERSION)),
            generated_at=data.get("generated_at", ""),
            spotify_user_id=data.get("spotify_user_id", ""),
            stats=data.get("stats", {}),
        )
        for p in data.get("playlists", []):
            m.playlists.append(
                PlaylistEntry(
                    spotify_id=p["spotify_id"],
                    name=p["name"],
                    owner=p.get("owner", ""),
                    description=p.get("description", ""),
                    public=bool(p.get("public", False)),
                    collaborative=bool(p.get("collaborative", False)),
                    track_count=int(p.get("track_count", 0)),
                    tracks=[_track_from_dict(t) for t in p.get("tracks", [])],
                )
            )
        for a in data.get("artists", []):
            m.artists.append(
                ArtistEntry(
                    spotify_id=a["spotify_id"],
                    name=a["name"],
                    genres=list(a.get("genres", [])),
                    popularity=int(a.get("popularity", 0)),
                    followers=int(a.get("followers", 0)),
                    tidal_id=a.get("tidal_id"),
                    tidal_name=a.get("tidal_name"),
                    match_score=a.get("match_score"),
                    matched=bool(a.get("matched", False)),
                    error=a.get("error"),
                    albums=[_album_from_dict(al) for al in a.get("albums", [])],
                )
            )
        return m

    # ---- I/O ----

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load_json(cls, path: Path) -> "Manifest":
        return cls.from_dict(json.loads(path.read_text()))

    def save_csv(self, path: Path) -> None:
        """Export a flat CSV of the most useful columns. Lossy by design."""
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "kind",
                    "playlist",
                    "artist",
                    "name",
                    "artists",
                    "album",
                    "duration_ms",
                    "spotify_id",
                    "isrc",
                    "tidal_id",
                    "tidal_url",
                    "matched",
                    "match_score",
                    "error",
                ]
            )
            for pl in self.playlists:
                for t in pl.tracks:
                    w.writerow(
                        [
                            "track",
                            pl.name,
                            "",
                            t.name,
                            ", ".join(t.artists),
                            t.album,
                            t.duration_ms,
                            t.spotify_id,
                            t.isrc,
                            t.tidal_id or "",
                            f"https://tidal.com/browse/track/{t.tidal_id}" if t.tidal_id else "",
                            "yes" if t.matched else "no",
                            t.match_score if t.match_score is not None else "",
                            t.error or "",
                        ]
                    )
            for ar in self.artists:
                for al in ar.albums:
                    w.writerow(
                        [
                            "album",
                            "",
                            ar.name,
                            al.name,
                            ", ".join(al.artists),
                            al.name,
                            "",
                            al.spotify_id,
                            "",
                            al.tidal_id or "",
                            f"https://tidal.com/browse/album/{al.tidal_id}" if al.tidal_id else "",
                            "yes" if al.matched else "no",
                            al.match_score if al.match_score is not None else "",
                            al.error or "",
                        ]
                    )

    # ---- summary ----

    def compute_stats(self) -> None:
        pl_tracks = sum(len(p.tracks) for p in self.playlists)
        pl_matched = sum(sum(1 for t in p.tracks if t.matched) for p in self.playlists)
        ar_albums = sum(len(a.albums) for a in self.artists)
        ar_matched_albums = sum(sum(1 for al in a.albums if al.matched) for a in self.artists)
        ar_matched = sum(1 for a in self.artists if a.matched)
        self.stats = {
            "playlists": len(self.playlists),
            "playlist_tracks": pl_tracks,
            "playlist_tracks_matched": pl_matched,
            "playlist_tracks_match_pct": _pct(pl_matched, pl_tracks),
            "artists": len(self.artists),
            "artists_matched": ar_matched,
            "artist_albums": ar_albums,
            "artist_albums_matched": ar_matched_albums,
            "artist_albums_match_pct": _pct(ar_matched_albums, ar_albums),
        }


def _pct(n: int, total: int) -> float:
    if not total:
        return 0.0
    return round(100.0 * n / total, 1)


def _track_from_dict(d: dict) -> TrackEntry:
    return TrackEntry(
        spotify_id=d["spotify_id"],
        spotify_uri=d.get("spotify_uri", ""),
        name=d["name"],
        duration_ms=int(d.get("duration_ms", 0)),
        artists=list(d.get("artists", [])),
        album=d.get("album", ""),
        album_id=d.get("album_id", ""),
        isrc=d.get("isrc", ""),
        explicit=bool(d.get("explicit", False)),
        tidal_id=d.get("tidal_id"),
        tidal_title=d.get("tidal_title"),
        tidal_artist=d.get("tidal_artist"),
        tidal_album=d.get("tidal_album"),
        tidal_album_id=d.get("tidal_album_id"),
        tidal_duration=d.get("tidal_duration"),
        match_score=d.get("match_score"),
        match_reasons=d.get("match_reasons"),
        matched=bool(d.get("matched", False)),
        error=d.get("error"),
        qobuz_id=d.get("qobuz_id"),
        qobuz_title=d.get("qobuz_title"),
        qobuz_artist=d.get("qobuz_artist"),
        qobuz_album=d.get("qobuz_album"),
        qobuz_album_id=d.get("qobuz_album_id"),
        qobuz_duration=d.get("qobuz_duration"),
    )


def _album_from_dict(d: dict) -> AlbumEntry:
    return AlbumEntry(
        spotify_id=d["spotify_id"],
        spotify_uri=d.get("spotify_uri", ""),
        name=d["name"],
        artists=list(d.get("artists", [])),
        album_type=d.get("album_type", "album"),
        total_tracks=int(d.get("total_tracks", 0)),
        release_date=d.get("release_date", ""),
        tidal_id=d.get("tidal_id"),
        tidal_title=d.get("tidal_title"),
        tidal_artist=d.get("tidal_artist"),
        tidal_release_date=d.get("tidal_release_date"),
        tidal_num_tracks=d.get("tidal_num_tracks"),
        match_score=d.get("match_score"),
        match_reasons=d.get("match_reasons"),
        matched=bool(d.get("matched", False)),
        error=d.get("error"),
        qobuz_id=d.get("qobuz_id"),
        qobuz_title=d.get("qobuz_title"),
        qobuz_artist=d.get("qobuz_artist"),
        qobuz_release_date=d.get("qobuz_release_date"),
        qobuz_num_tracks=d.get("qobuz_num_tracks"),
    )


# ---------- builders from Spotify API objects ----------

def track_from_spotify(t: SpotifyTrack) -> TrackEntry:
    return TrackEntry(
        spotify_id=t.id,
        spotify_uri=t.uri,
        name=t.name,
        duration_ms=t.duration_ms,
        artists=[a.name for a in t.artists if a and a.name],
        album=t.album.name if t.album else "",
        album_id=t.album.id if t.album else "",
        isrc=t.isrc,
        explicit=t.explicit,
    )


def album_from_spotify(a: SpotifyAlbum) -> AlbumEntry:
    return AlbumEntry(
        spotify_id=a.id,
        spotify_uri=a.uri,
        name=a.name,
        artists=[x.name for x in a.artists if x and x.name],
        album_type=a.album_type,
        total_tracks=a.total_tracks,
        release_date=a.release_date,
    )


def artist_from_spotify(a: SpotifyArtist) -> ArtistEntry:
    return ArtistEntry(
        spotify_id=a.id,
        name=a.name,
        genres=a.genres,
        popularity=a.popularity,
        followers=a.followers,
    )


def playlist_summary(p: SpotifyPlaylistSummary) -> PlaylistEntry:
    return PlaylistEntry(
        spotify_id=p.id,
        name=p.name,
        owner=p.owner,
        description=p.description,
        public=p.public,
        collaborative=p.collaborative,
        track_count=p.track_count,
    )


def new_manifest(spotify_user_id: str) -> Manifest:
    return Manifest(
        version=MANIFEST_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        spotify_user_id=spotify_user_id,
    )
