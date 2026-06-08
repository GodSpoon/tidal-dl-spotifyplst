"""SquidWTF (qobuz.squid.wtf) headless downloader backend.

Pure-HTTP downloader that hits the qobuz.squid.wtf Next.js API to fetch
Qobuz download URLs and streams them directly to disk. No browser, no
CLI tools — just requests + ThreadPoolExecutor for parallel throughput.
"""
from __future__ import annotations

import collections
import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any

import requests

from ..config import AppConfig
from ..manifest import Manifest
from . import DownloaderBackend
from ..progress import get_tracker

log = logging.getLogger(__name__)
_BASE = "https://qobuz.squid.wtf"
_SEARCH = f"{_BASE}/api/get-music"
_ALBUM = f"{_BASE}/api/get-album"
_DOWNLOAD = f"{_BASE}/api/download-music"

# Qobuz quality IDs used by the squid.wtf frontend:
#   27 = 24-bit/96kHz FLAC
#   7  = 16-bit/44.1kHz FLAC  ← default (CD quality)
#   6  = 320kbps MP3
#   5  = 128kbps MP3
_QUALITY_MAP: dict[str, str] = {
    "max": "7",     # CD quality — user prefers 16/44 as the ceiling
    "high": "7",    # CD quality
    "normal": "6",  # 320kbps MP3 (minimum acceptable)
    "low": "6",     # 320kbps MP3 (floor — never go lower)
}

# Retry settings for transient network / rate-limit errors
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds


def _safe_name(name: str) -> str:
    """Sanitise a file-system name."""
    keep = " ._-"
    return "".join(c if c.isalnum() or c in keep else "_" for c in name).strip(" ._")


def _download_url(
    track_id: int,
    quality: str,
    session: requests.Session,
) -> str:
    """Resolve a track ID to a signed Qobuz download URL via squid.wtf."""
    resp = session.get(
        _DOWNLOAD,
        params={"track_id": track_id, "quality": quality},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"API error for track {track_id}: {payload.get('error')}")
    return payload["data"]["url"]


def _stream_to_disk(
    url: str,
    dest: Path,
    session: requests.Session,
) -> None:
    """Stream *url* to *dest* in 1 MiB chunks."""
    resp = session.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)


def _fetch_album_tracks(album_id: str, session: requests.Session) -> list[dict[str, Any]]:
    """Return the track list for a Qobuz album via squid.wtf."""
    resp = session.get(_ALBUM, params={"album_id": album_id}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"API error for album {album_id}: {payload.get('error')}")
    tracks = payload["data"].get("tracks", {}).get("items", [])
    return tracks


def _build_download_plan(manifest: Manifest) -> list[dict[str, Any]]:
    """Flatten the manifest into a list of per-track download tasks.

    Playlist tracks that share an album are resolved at the album level
    (one API call) so we can grab every track in one shot.
    """
    tasks: list[dict[str, Any]] = []

    # ---------- Playlists ----------
    for pl in manifest.playlists:
        # Group matched tracks by qobuz_album_id
        by_album: dict[str, list] = collections.defaultdict(list)
        solo_tracks: list = []
        for t in pl.tracks:
            if not t.matched:
                continue
            if t.qobuz_album_id:
                by_album[t.qobuz_album_id].append(t)
            elif t.qobuz_id:
                solo_tracks.append(t)

        # Albums with ≥2 tracks → fetch full album, download all its tracks
        for album_id, tracks in by_album.items():
            tasks.append({
                "type": "album",
                "album_id": album_id,
                "artist": tracks[0].artists[0] if tracks[0].artists else "Unknown",
                "album": tracks[0].album,
                "source": f"playlist:{pl.name}",
            })

        # Lone tracks → download individually
        for t in solo_tracks:
            tasks.append({
                "type": "track",
                "track_id": int(t.qobuz_id),
                "artist": t.artists[0] if t.artists else "Unknown",
                "album": t.album,
                "title": t.name,
                "track_number": 1,
                "source": f"playlist:{pl.name}",
            })

    # ---------- Artist albums ----------
    for a in manifest.artists:
        if not a.matched:
            continue
        for al in a.albums:
            if al.matched and al.qobuz_id:
                tasks.append({
                    "type": "album",
                    "album_id": al.qobuz_id,
                    "artist": a.name,
                    "album": al.name,
                    "source": f"artist:{a.name}",
                })
    return tasks


def _download_track(
    task: dict[str, Any],
    quality: str,
    out_dir: Path,
    session: requests.Session,
    tracker_name: str,
) -> tuple[bool, str]:
    """Download a single track. Returns (ok, description)."""
    artist = _safe_name(task["artist"])
    album = _safe_name(task["album"])
    title = _safe_name(task["title"])
    track_num = task.get("track_number", 1)
    track_id = task["track_id"]

    dest = out_dir / artist / album / f"{track_num:02d} {title}.flac"
    if dest.exists() and dest.stat().st_size > 0:
        return True, f"SKIP (exists): {dest.name}"

    for attempt in range(_MAX_RETRIES):
        try:
            url = _download_url(track_id, quality, session)
            _stream_to_disk(url, dest, session)
            return True, f"OK: {dest.name}"
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response else 0
            if status == 429:
                backoff = _BACKOFF_BASE * (2 ** attempt)
                time.sleep(backoff)
                continue
            return False, f"FAIL ({status}): {title} — {exc}"
        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE * (2 ** attempt))
                continue
            return False, f"FAIL: {title} — {exc}"

    return False, f"FAIL (max retries): {title}"


def _download_album(
    task: dict[str, Any],
    quality: str,
    out_dir: Path,
    session: requests.Session,
    tracker_name: str,
) -> list[tuple[bool, str]]:
    """Fetch album track list and download every track."""
    album_id = task["album_id"]
    artist = _safe_name(task["artist"])
    album = _safe_name(task["album"])

    try:
        tracks = _fetch_album_tracks(album_id, session)
    except Exception as exc:
        return [(False, f"FAIL (album fetch {album_id}): {exc}")]

    results: list[tuple[bool, str]] = []
    for tr in tracks:
        sub_task = {
            "type": "track",
            "track_id": tr["id"],
            "artist": task["artist"],
            "album": task["album"],
            "title": tr.get("title", "Unknown"),
            "track_number": tr.get("track_number", 1),
            "source": task["source"],
        }
        results.append(_download_track(sub_task, quality, out_dir, session, tracker_name))
    return results


class SquidWtfDownloader(DownloaderBackend):
    """Headless downloader via qobuz.squid.wtf API."""

    @property
    def name(self) -> str:
        return "squidwtf"

    def download(
        self,
        manifest: Manifest,
        cfg: AppConfig,
        **kwargs: Any,
    ) -> int:
        quality_label = kwargs.get("quality", cfg.tidal_quality or "max")
        quality = _QUALITY_MAP.get(quality_label, "27")
        max_workers = kwargs.get("max_workers", 8)

        out_dir = Path(cfg.tidal_download_dir) / "squidwtf"
        out_dir.mkdir(parents=True, exist_ok=True)

        plan = _build_download_plan(manifest)
        if not plan:
            print("[squidwtf] Nothing to download.")
            return 0

        tracker = get_tracker()
        tracker.register_backend(self.name, total=len(plan))

        print(f"[squidwtf] Downloading {len(plan)} item(s) → {out_dir}")
        print(f"[squidwtf] Quality: {quality_label} (Qobuz format_id={quality})")

        session = requests.Session()
        session.headers.update({
            "User-Agent": "spotify_to_tidal/squidwtf",
            "Accept": "application/json",
        })

        last_rc = 0
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs: dict[concurrent.futures.Future, dict[str, Any]] = {}
            for task in plan:
                if task["type"] == "album":
                    fut = pool.submit(
                        _download_album, task, quality, out_dir, session, self.name
                    )
                else:
                    fut = pool.submit(
                        _download_track, task, quality, out_dir, session, self.name
                    )
                futs[fut] = task

            for fut in concurrent.futures.as_completed(futs):
                task = futs[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    log.error("squidwtf task raised: %s", exc, exc_info=True)
                    result = (False, f"FAIL (exception): {exc}")

                # result is either a single tuple or a list of tuples (albums)
                if isinstance(result, list):
                    ok_count = sum(1 for ok, _ in result if ok)
                    for ok, msg in result:
                        status = "OK" if ok else "FAIL"
                        print(f"[squidwtf] [{status}] {msg}")
                    if ok_count < len(result):
                        last_rc = 1
                else:
                    ok, msg = result
                    status = "OK" if ok else "FAIL"
                    print(f"[squidwtf] [{status}] {msg}")
                    if not ok:
                        last_rc = 1

                completed += 1
                tracker.update_backend(self.name, completed=completed, current=task.get("album_id") or task.get("track_id"))

        tracker.update_backend(self.name, status="done" if last_rc == 0 else "error")
        return last_rc
