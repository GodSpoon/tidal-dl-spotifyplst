"""Thin Qobuz API v0.2 client.

Authentication: app_id + user auth-token pair acquired via the Qobuz API
login endpoint (not implemented here; token is read from config).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

_BASE = "https://www.qobuz.com/api.json/0.2/"


@dataclass
class QobuzTrack:
    id: str
    title: str
    artist: str
    album: str
    duration_ms: int
    album_id: str
    isrc: str = ""
    explicit: bool = False
@dataclass
class QobuzAlbum:
    id: str
    title: str
    artist: str
    release_date: str
    total_tracks: int

class QobuzClient:
    """Minimal Qobuz search client with in-memory result cache."""

    def __init__(self, app_id: str, auth_token: str) -> None:
        self._app_id = app_id
        self._auth_token = auth_token
        # keyed by (query, "tracks"|"albums")
        self._cache: dict[tuple[str, str], list[Any]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        full_params: dict[str, Any] = {"app_id": self._app_id, **params}
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["X-User-Auth-Token"] = self._auth_token
        resp = requests.get(
            _BASE + endpoint,
            params=full_params,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Public search API
    # ------------------------------------------------------------------

    def search_tracks(self, query: str, limit: int = 10) -> list[QobuzTrack]:
        """Search Qobuz for tracks matching *query*."""
        key = (query, "tracks")
        if key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        try:
            data = self._get("track/search", {"query": query, "limit": limit})
            tracks: list[QobuzTrack] = []
            for item in data.get("tracks", {}).get("items", []):
                album_obj = item.get("album") or {}
                performer = item.get("performer") or {}
                tracks.append(
                    QobuzTrack(
                        id=str(item.get("id", "")),
                        title=item.get("title", ""),
                        artist=performer.get("name", ""),
                        album=album_obj.get("title", ""),
                        duration_ms=int(item.get("duration", 0)) * 1000,
                        album_id=str(album_obj.get("id", "")),
                        isrc=(item.get("isrc") or ""),
                        explicit=bool(item.get("explicit", False)),
                    )
                )
            self._cache[key] = tracks  # type: ignore[assignment]
            return tracks
        except requests.HTTPError as exc:
            print(f"[qobuz] track search HTTP error: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001
            print(f"[qobuz] track search error: {exc}")
            return []

    def search_albums(self, query: str, limit: int = 10) -> list[QobuzAlbum]:
        """Search Qobuz for albums matching *query*."""
        key = (query, "albums")
        if key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        try:
            data = self._get("album/search", {"query": query, "limit": limit})
            albums: list[QobuzAlbum] = []
            for item in data.get("albums", {}).get("items", []):
                artist_obj = item.get("artist") or {}
                release_date = (
                    item.get("release_date_original")
                    or item.get("release_date_download")
                    or ""
                )
                albums.append(
                    QobuzAlbum(
                        id=str(item.get("id", "")),
                        title=item.get("title", ""),
                        artist=artist_obj.get("name", ""),
                        release_date=release_date,
                        total_tracks=int(item.get("tracks_count", 0)),
                    )
                )
            self._cache[key] = albums  # type: ignore[assignment]
            return albums
        except requests.HTTPError as exc:
            print(f"[qobuz] album search HTTP error: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001
            print(f"[qobuz] album search error: {exc}")
            return []
