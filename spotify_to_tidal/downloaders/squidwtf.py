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
from typing import Any, Callable

import requests

from ..config import AppConfig
from ..manifest import Manifest
from . import DownloaderBackend

try:
    from ..progress import get_tracker
except Exception:
    get_tracker = None  # progress module is optional

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
    "max": "7",     # CD quality (16/44 FLAC)
    "high": "7",    # CD quality
    "normal": "6",  # 320kbps MP3
    "low": "5",     # 128kbps MP3
}

# Format IDs that are lossless
_LOSSLESS_QUALITIES = {"7", "27"}
_LOSSLESS_EXT = ".flac"
_LOSSY_EXT = ".mp3"

# Retry settings for transient network / rate-limit errors
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds


def _safe_name(name: str) -> str:
    """Sanitise a file-system name."""
    keep = " ._-"
    return "".join(c if c.isalnum() or c in keep else "_" for c in name).strip(" ._")


def _ext_for_quality(quality: str) -> str:
    """Return the right file extension for a Qobuz format_id."""
    if quality in _LOSSLESS_QUALITIES:
        return _LOSSLESS_EXT
    return _LOSSY_EXT


def _download_url(track_id: int, quality: str, session: requests.Session) -> str:
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
    data = payload.get("data")
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"API missing data for track {track_id}: {payload}")
    url = data.get("url")
    if not url:
        raise RuntimeError(f"API missing URL for track {track_id}: {payload}")
    return url



def _stream_to_disk(
    url: str,
    dest: Path,
    session: requests.Session,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """Stream *url* to *dest* in 1 MiB chunks. Returns bytes written.

    *on_bytes*, if given, is invoked with each chunk's byte count so
    callers can measure throughput.
    """
    resp = session.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        with part.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))
        part.replace(dest)
    except Exception:
        # Leave the .part file in place so the next run knows the download
        # was interrupted; do not create a truncated final file.
        raise
    return written


def _fetch_album_tracks(album_id: str, session: requests.Session) -> list[dict[str, Any]]:
    """Return the track list for a Qobuz album via squid.wtf."""
    resp = session.get(_ALBUM, params={"album_id": album_id}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"API error for album {album_id}: {payload.get('error')}")
    data = payload.get("data")
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"API missing data for album {album_id}: {payload}")
    return data.get("tracks", {}).get("items", [])

def _build_download_plan(manifest: Manifest) -> list[dict[str, Any]]:
    """Flatten the manifest into a list of per-track download tasks."""
    tasks: list[dict[str, Any]] = []

    # ---------- Playlists ----------
    for pl in manifest.playlists:
        by_album: dict[str, list] = collections.defaultdict(list)
        solo_tracks: list = []
        for t in pl.tracks:
            if not t.matched:
                continue
            if t.qobuz_album_id:
                by_album[t.qobuz_album_id].append(t)
            elif t.qobuz_id:
                solo_tracks.append(t)

        for album_id, tracks in by_album.items():
            tasks.append({
                "type": "album",
                "album_id": album_id,
                "artist": tracks[0].artists[0] if tracks[0].artists else "Unknown",
                "album": tracks[0].album,
                "source": f"playlist:{pl.name}",
            })

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
    on_bytes: Callable[[int], None] | None = None,
) -> tuple[bool, str, str]:
    """Download a single track. Returns (ok, description, status_tag).

    *status_tag* is one of "ok", "skip", "fail" — used by the caller to
    update the progress tracker with the right counter.
    """
    artist = _safe_name(task["artist"])
    album = _safe_name(task["album"])
    title = _safe_name(task["title"])
    track_num = task.get("track_number", 1)
    track_id = task["track_id"]
    ext = _ext_for_quality(quality)

    dest = out_dir / artist / album / f"{track_num:02d} {title}{ext}"
    if dest.exists() and dest.stat().st_size > 0:
        return True, f"SKIP (exists): {dest.name}", "skip"

    for attempt in range(_MAX_RETRIES):
        try:
            url = _download_url(track_id, quality, session)
            _stream_to_disk(url, dest, session, on_bytes=on_bytes)
            return True, f"OK: {dest.name}", "ok"
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response else 0
            if status == 429 and attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE * (2 ** attempt))
                continue
            return False, f"FAIL ({status}): {title} — {exc}", "fail"
        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE * (2 ** attempt))
                continue
            return False, f"FAIL: {title} — {exc}", "fail"

    return False, f"FAIL (max retries): {title}", "fail"


def _download_album(
    task: dict[str, Any],
    quality: str,
    out_dir: Path,
    session: requests.Session,
    on_bytes: Callable[[int], None] | None = None,
) -> list[tuple[bool, str, str]]:
    """Fetch album track list and download every track."""
    album_id = task["album_id"]
    try:
        tracks = _fetch_album_tracks(album_id, session)
    except Exception as exc:
        return [(False, f"FAIL (album fetch {album_id}): {exc}", "fail")]

    results: list[tuple[bool, str, str]] = []
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
        results.append(_download_track(sub_task, quality, out_dir, session, on_bytes))
    return results


def _expand_plan(
    plan: list[dict[str, Any]],
    session: requests.Session,
    max_workers: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Expand album tasks to per-track tasks in parallel.

    Returns (track_tasks, error_messages).  Solo-track tasks pass through
    unchanged.  Album metadata fetches run up to *max_workers* at a time so
    the whole expansion finishes before any download starts.
    """
    album_tasks = [t for t in plan if t["type"] == "album"]
    solo_tracks = [t for t in plan if t["type"] == "track"]

    expanded: list[dict[str, Any]] = list(solo_tracks)
    errors: list[str] = []

    if not album_tasks:
        return expanded, errors

    fetch_workers = min(len(album_tasks), max_workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        futs = {
            pool.submit(_fetch_album_tracks, t["album_id"], session): t
            for t in album_tasks
        }
        for fut in concurrent.futures.as_completed(futs):
            task = futs[fut]
            try:
                tracks = fut.result()
            except Exception as exc:
                msg = f"FAIL (album fetch {task['album_id']}): {exc}"
                errors.append(msg)
                log.error("squidwtf album fetch: %s", msg)
                continue
            for tr in tracks:
                expanded.append({
                    "type": "track",
                    "track_id": tr["id"],
                    "artist": task["artist"],
                    "album": task["album"],
                    "title": tr.get("title", "Unknown"),
                    "track_number": tr.get("track_number", 1),
                    "source": task["source"],
                })

    return expanded, errors


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
        quality = _QUALITY_MAP.get(quality_label, "7")
        max_workers: int = kwargs.get("max_workers", 16)

        out_dir = Path(cfg.tidal_download_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        plan = _build_download_plan(manifest)
        if not plan:
            print(f"[{self.name}] Nothing to download.")
            return 0

        # One session shared across all threads; HTTPAdapter pool sized to workers.
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max_workers,
            pool_maxsize=max_workers,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": "spotify_to_tidal/squidwtf",
            "Accept": "application/json",
        })

        tracker = get_tracker() if get_tracker else None
        total = len(plan)
        if tracker:
            tracker.register_backend(self.name, total=total)

        def on_bytes(n: int) -> None:
            if tracker:
                tracker.add_bytes(self.name, n)

        album_count = sum(1 for t in plan if t["type"] == "album")
        track_count = sum(1 for t in plan if t["type"] == "track")
        print(f"[{self.name}] {album_count} album task(s), {track_count} track task(s) -> {out_dir}")
        print(f"[{self.name}] Quality: {quality_label} (id={quality}, ext={_ext_for_quality(quality)}, workers={max_workers})")

        last_rc = 0
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs: dict[concurrent.futures.Future, dict[str, Any]] = {}
            for task in plan:
                if task["type"] == "album":
                    fut = pool.submit(_download_album, task, quality, out_dir, session, on_bytes)
                else:
                    fut = pool.submit(_download_track, task, quality, out_dir, session, on_bytes)
                futs[fut] = task

            for fut in concurrent.futures.as_completed(futs):
                task = futs[fut]
                task_label = f"{task.get('artist', '?')}/{task.get('album', '?')}"
                try:
                    result = fut.result()
                except Exception as exc:
                    log.error("squidwtf task raised: %s", exc, exc_info=True)
                    result = (False, f"FAIL (exception): {exc}", "fail")

                # Album tasks return a list of per-track results; track tasks return a single tuple.
                if isinstance(result, list):
                    results = result
                else:
                    results = [result]

                for ok, msg, tag in results:
                    if tracker:
                        if tag == "skip":
                            tracker.increment(self.name, skipped=1)
                        elif tag == "ok":
                            tracker.increment(self.name, completed=1)
                        else:
                            tracker.increment(self.name, failed=1)
                    if not ok:
                        last_rc = 1
                    if tracker:
                        tracker.log_event(self.name, msg)
                    else:
                        print(f"[{self.name}] [{'OK' if ok else 'FAIL'}] {msg}")

                completed += 1
                if tracker:
                    tracker.update_backend(self.name, completed=completed, current=task_label)

        if tracker:
            tracker.mark_done(self.name, error=(last_rc != 0))
        return last_rc
