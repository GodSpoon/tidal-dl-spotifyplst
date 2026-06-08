"""Tidarr API backend.

Pushes URLs to a running Tidarr instance and monitors the queue until
all items finish (or error).
"""
from __future__ import annotations

import collections
import time
from typing import TYPE_CHECKING

import requests

from ..manifest import Manifest
from . import DownloaderBackend

if TYPE_CHECKING:
    from ..config import AppConfig

_TIDAL_BASE = "https://tidal.com/browse"
_POST_DELAY = 0.2        # seconds between successive POSTs
_POLL_INTERVAL = 10      # seconds between queue polls
_TIMEOUT = 86_400        # 24 hours in seconds


def _build_url_list(manifest: Manifest) -> list[tuple[str, str]]:
    """Return [(url, type), ...] using the same album-grouping as tiddl backend.

    Rules (mirroring build_tidal_input_file):
    - Playlist tracks sharing a tidal_album_id with ≥2 matched tracks → album URL once.
    - Remaining playlist tracks → individual track URLs.
    - Artist albums (matched, has tidal_id) → album URLs.
    """
    items: list[tuple[str, str]] = []

    # Playlists — group by Tidal album id.
    for pl in manifest.playlists:
        album_track_count: dict[int, int] = collections.defaultdict(int)
        for t in pl.tracks:
            if t.matched and t.tidal_id and t.tidal_album_id:
                album_track_count[t.tidal_album_id] += 1

        emitted_albums: set[int] = set()
        for t in pl.tracks:
            if not (t.matched and t.tidal_id):
                continue
            aid = t.tidal_album_id
            if aid and album_track_count[aid] >= 2:
                if aid not in emitted_albums:
                    items.append((f"{_TIDAL_BASE}/album/{aid}", "album"))
                    emitted_albums.add(aid)
            else:
                items.append((f"{_TIDAL_BASE}/track/{t.tidal_id}", "track"))

    # Artist albums — always album URLs.
    for a in manifest.artists:
        if not a.matched:
            continue
        for al in a.albums:
            if al.matched and al.tidal_id:
                items.append((f"{_TIDAL_BASE}/album/{al.tidal_id}", "album"))

    return items


class TidarrDownloader(DownloaderBackend):
    @property
    def name(self) -> str:
        return "tidarr"

    def download(self, manifest: Manifest, cfg: "AppConfig", **_kwargs) -> int:
        url_list = _build_url_list(manifest)
        if not url_list:
            print("[tidarr] Nothing to queue.")
            return 0

        base_url: str = cfg.tidarr_url.rstrip("/")
        api_key: str | None = cfg.tidarr_api_key
        headers = {"X-Api-Key": api_key or "", "Content-Type": "application/json"}

        # --- Queue phase ---
        print(f"[tidarr] Queuing {len(url_list)} item(s) → {base_url}")
        queued_urls: set[str] = set()
        for url, item_type in url_list:
            payload = {
                "item": {
                    "url": url,
                    "type": item_type,
                    "status": "queue_download",
                }
            }
            try:
                resp = requests.post(
                    f"{base_url}/api/save",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                queued_urls.add(url)
                print(f"[tidarr]  queued {item_type}: {url}")
            except requests.HTTPError as exc:
                print(f"[tidarr] HTTP error queuing {url}: {exc}")
                return 1
            except requests.RequestException as exc:
                print(f"[tidarr] Request error queuing {url}: {exc}")
                return 1
            time.sleep(_POST_DELAY)

        if not queued_urls:
            return 1

        # --- Poll phase ---
        print(f"[tidarr] Polling queue every {_POLL_INTERVAL}s (timeout 24 h)…")
        deadline = time.monotonic() + _TIMEOUT
        poll_headers = {"X-Api-Key": api_key or ""}

        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL)
            try:
                resp = requests.get(
                    f"{base_url}/api/queue/list",
                    headers=poll_headers,
                    timeout=30,
                )
                resp.raise_for_status()
                queue_data = resp.json()
            except requests.HTTPError as exc:
                print(f"[tidarr] HTTP error polling queue: {exc}")
                return 1
            except requests.RequestException as exc:
                print(f"[tidarr] Request error polling queue: {exc}")
                return 1
            except ValueError as exc:
                print(f"[tidarr] Invalid JSON from queue/list: {exc}")
                return 1

            # queue_data may be a list or {"items": [...]}
            if isinstance(queue_data, list):
                all_items = queue_data
            elif isinstance(queue_data, dict):
                all_items = queue_data.get("items", queue_data.get("queue", []))
            else:
                all_items = []

            # Filter to only items we submitted (by URL match).
            our_items = [
                item for item in all_items
                if isinstance(item, dict) and item.get("url") in queued_urls
            ]

            total = len(our_items)
            finished = sum(1 for i in our_items if i.get("status") == "finished")
            errors = sum(1 for i in our_items if i.get("status") == "error")
            pending = total - finished - errors

            print(
                f"[tidarr] progress — total: {total}, "
                f"finished: {finished}, error: {errors}, pending: {pending}"
            )

            if total > 0 and pending == 0:
                break

            # If our items haven't appeared in the queue yet, keep waiting.

        else:
            print("[tidarr] Timed out waiting for queue to complete.")
            return 1

        if errors:
            print(f"[tidarr] Done with {errors} error(s).")
            return 1

        print("[tidarr] All items finished successfully.")
        return 0
