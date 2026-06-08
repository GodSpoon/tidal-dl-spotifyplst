"""Spotify Web API client.

Wraps requests with retry + pagination helpers. We avoid the spotipy
dependency because (a) it isn't installed and (b) we only need ~6 endpoints.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import requests

from .auth import SpotifyToken

API = "https://api.spotify.com/v1"
MAX_LIMIT = 50  # Spotify default max per page


@dataclass
class SpotifyImage:
    url: str
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class SpotifyArtist:
    id: str
    name: str
    uri: str
    genres: list[str] = field(default_factory=list)
    popularity: int = 0
    followers: int = 0
    images: list[SpotifyImage] = field(default_factory=list)


@dataclass
class SpotifyAlbum:
    id: str
    name: str
    uri: str
    album_type: str  # album / single / compilation
    artists: list[SpotifyArtist] = field(default_factory=list)
    total_tracks: int = 0
    release_date: str = ""
    images: list[SpotifyImage] = field(default_factory=list)
    upc: str = ""


@dataclass
class SpotifyTrack:
    id: str
    name: str
    uri: str
    duration_ms: int
    artists: list[SpotifyArtist] = field(default_factory=list)
    album: Optional[SpotifyAlbum] = None
    isrc: str = ""
    explicit: bool = False
    popularity: int = 0
    preview_url: str = ""


@dataclass
class SpotifyPlaylist:
    id: str
    name: str
    uri: str
    description: str
    owner: str
    public: bool
    collaborative: bool
    track_count: int
    images: list[SpotifyImage] = field(default_factory=list)
    tracks: list[SpotifyTrack] = field(default_factory=list)


@dataclass
class SpotifyPlaylistSummary:
    """Lightweight playlist metadata for the manifest top-level."""
    id: str
    name: str
    uri: str
    description: str
    owner: str
    public: bool
    collaborative: bool
    track_count: int
    images: list[SpotifyImage] = field(default_factory=list)


# ---------- low-level transport ----------

class SpotifyClient:
    def __init__(self, token: SpotifyToken):
        self._token = token
        self._sess = requests.Session()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.access_token}"}

    def _request(self, method: str, path: str, **params) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{API}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(5):
            try:
                r = self._sess.request(
                    method, url, headers=self._auth_headers(), params=params, timeout=20
                )
            except requests.RequestException as e:
                last_exc = e
                time.sleep(0.5 * (2 ** attempt))
                continue
            if r.status_code == 429:
                # Rate limited - respect Retry-After
                wait = int(r.headers.get("Retry-After", "1"))
                time.sleep(min(wait, 10))
                last_exc = RuntimeError(f"Rate limited (Retry-After: {wait}s)")
                continue
            if r.status_code >= 500:
                time.sleep(0.5 * (2 ** attempt))
                last_exc = RuntimeError(f"{r.status_code} {r.text[:200]}")
                continue
            if r.status_code == 401:
                raise PermissionError(
                    "Spotify access token invalid/expired and refresh failed."
                )
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Spotify {method} {path} failed: {last_exc}")

    def _paginate(self, path: str, key: str, **params) -> Iterator[dict]:
        """Yield items from a paginated Spotify endpoint."""
        params = {**params, "limit": MAX_LIMIT}
        while path:
            data = self._request("GET", path, **params)
            for item in data.get(key) or []:
                yield item
            path = data.get("next")
            # subsequent calls via `next` carry the full query string already;
            # clear our own params so we don't append them twice
            params = {}

    # ---------- mapping helpers ----------

    @staticmethod
    def _l(d: dict, key: str) -> list:
        """Return ``d[key]`` if it's a list, else ``[]``.

        ``dict.get`` only substitutes the default when the key is absent;
        Spotify occasionally returns ``null`` for array fields, so we
        explicitly coalesce those cases.
        """
        v = d.get(key)
        return v if isinstance(v, list) else []

    @staticmethod
    def _img(d: dict) -> SpotifyImage:
        return SpotifyImage(
            url=d.get("url", ""),
            width=d.get("width"),
            height=d.get("height"),
        )

    @staticmethod
    def _artist(d: dict) -> SpotifyArtist:
        return SpotifyArtist(
            id=d["id"],
            name=d["name"],
            uri=d["uri"],
            genres=d.get("genres", []) or [],
            popularity=int(d.get("popularity", 0)),
            followers=int(((d.get("followers") or {}).get("total")) or 0),
            images=[SpotifyClient._img(i) for i in SpotifyClient._l(d, "images")],
        )

    @classmethod
    def _album(cls, d: dict) -> SpotifyAlbum:
        return SpotifyAlbum(
            id=d["id"],
            name=d["name"],
            uri=d["uri"],
            album_type=d.get("album_type", "album"),
            artists=[cls._artist(a) for a in cls._l(d, "artists")],
            total_tracks=int(d.get("total_tracks", 0)),
            release_date=d.get("release_date", ""),
            images=[cls._img(i) for i in cls._l(d, "images")],
            upc=(d.get("external_ids") or {}).get("upc", ""),
        )

    @classmethod
    def _track(cls, d: dict) -> SpotifyTrack:
        return SpotifyTrack(
            id=d["id"],
            name=d["name"],
            uri=d["uri"],
            duration_ms=int(d.get("duration_ms", 0)),
            artists=[cls._artist(a) for a in cls._l(d, "artists")],
            album=cls._album(d["album"]) if d.get("album") else None,
            isrc=(d.get("external_ids") or {}).get("isrc", ""),
            explicit=bool(d.get("explicit", False)),
            popularity=int(d.get("popularity", 0)),
            preview_url=d.get("preview_url") or "",
        )

    # ---------- high-level calls ----------

    def current_user_id(self) -> str:
        return self._request("GET", "/me")["id"]

    def list_user_playlists(self, user_id: Optional[str] = None) -> list[SpotifyPlaylistSummary]:
        uid = user_id or self.current_user_id()
        out: list[SpotifyPlaylistSummary] = []
        for p in self._paginate(f"/users/{uid}/playlists", "items"):
            images = [self._img(i) for i in self._l(p, "images")]
            out.append(
                SpotifyPlaylistSummary(
                    id=p["id"],
                    name=p["name"],
                    uri=p["uri"],
                    description=p.get("description", "") or "",
                    owner=(p.get("owner") or {}).get("display_name", "")
                    or (p.get("owner") or {}).get("id", ""),
                    public=bool(p.get("public", False)),
                    collaborative=bool(p.get("collaborative", False)),
                    track_count=int(((p.get("tracks") or {}).get("total")) or 0),
                    images=images,
                )
            )
        return out

    def get_playlist(self, playlist_id: str) -> SpotifyPlaylist:
        meta = self._request("GET", f"/playlists/{playlist_id}")
        tracks: list[SpotifyTrack] = []
        for item in self._paginate(f"/playlists/{playlist_id}/tracks", "items"):
            if not item or not item.get("track") or item["track"].get("is_local"):
                continue
            tracks.append(self._track(item["track"]))
        return SpotifyPlaylist(
            id=meta["id"],
            name=meta["name"],
            uri=meta["uri"],
            description=meta.get("description", "") or "",
            owner=(meta.get("owner") or {}).get("display_name", "")
            or (meta.get("owner") or {}).get("id", ""),
            public=bool(meta.get("public", False)),
            collaborative=bool(meta.get("collaborative", False)),
            track_count=len(tracks),
            images=[self._img(i) for i in self._l(meta, "images")],
            tracks=tracks,
        )

    def list_top_artists(
        self, total: int = 500, time_range: str = "long_term"
    ) -> list[SpotifyArtist]:
        """Get up to `total` top artists across all ranges (long/medium/short)."""
        seen: dict[str, SpotifyArtist] = {}
        for tr in ("long_term", "medium_term", "short_term"):
            for d in self._paginate(
                "/me/top/artists", "items", time_range=tr, limit=MAX_LIMIT
            ):
                a = self._artist(d)
                if a.id not in seen:
                    seen[a.id] = a
            if len(seen) >= total:
                break

        # If we still have headroom, try /me/followed?type=artist as a bonus
        # (this requires user-follow-read which we didn't request, so it may 403)
        if len(seen) < total:
            try:
                for d in self._paginate("/me/following", "artists", type="artist"):
                    a = self._artist(d)
                    if a.id not in seen:
                        seen[a.id] = a
            except requests.HTTPError:
                pass

        artists = list(seen.values())
        # Sort by combined popularity for determinism
        artists.sort(key=lambda a: (a.popularity, a.followers), reverse=True)
        return artists[:total]

    def list_artist_albums(
        self, artist_id: str, include_groups: str = "album,single,compilation,appears_on"
    ) -> list[SpotifyAlbum]:
        out: list[SpotifyAlbum] = []
        for d in self._paginate(
            f"/artists/{artist_id}/albums",
            "items",
            include_groups=include_groups,
            market="from_token",
        ):
            out.append(self._album(d))
        # Dedupe by (name, release_date) since the API returns dupes
        seen: set[tuple[str, str]] = set()
        unique: list[SpotifyAlbum] = []
        for a in out:
            key = (a.name.lower(), a.release_date)
            if key in seen:
                continue
            seen.add(key)
            unique.append(a)
        return unique
