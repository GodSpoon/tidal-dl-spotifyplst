#!/usr/bin/env python3
"""Fast qobuz ID resolver — searches qobuz.squid.wtf for each unmatched track.

Writes updated manifest with qobuz_id / qobuz_album_id populated.
Run after `build` and before `download --downloader squidwtf`.
"""
from __future__ import annotations

import json
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_qobuz_ids")

_BASE = "https://qobuz.squid.wtf"
_SEARCH = f"{_BASE}/api/get-music"
_MAX_WORKERS = 8
_RETRY = 3


def _search_one(track: dict) -> tuple[dict, dict | None]:
    """Search qobuz for a Spotify track. Returns (track, qobuz_result or None)."""
    artist = (track.get("artists") or [""])[0]
    title = track.get("name", "")
    query = f"{artist} {title}".strip()
    session = requests.Session()
    session.headers["User-Agent"] = "spotify_to_tidal/qobuz-resolver"

    for attempt in range(_RETRY):
        try:
            resp = session.get(
                _SEARCH,
                params={"q": query, "offset": 0},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("success"):
                return track, None
            data = payload.get("data", {})
            tracks = data.get("tracks", {}).get("items", [])
            if tracks:
                return track, tracks[0]
            return track, None
        except Exception as exc:
            if attempt < _RETRY - 1:
                time.sleep(2 ** attempt)
            else:
                log.debug("Search failed for %s: %s", query, exc)
                return track, None
    return track, None


def main() -> int:
    manifest_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/manifest.json")
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else manifest_path

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Collect unmatched tracks
    unmatched: list[dict] = []
    seen_keys: set[str] = set()
    for pl in manifest.get("playlists", []):
        for t in pl.get("tracks", []):
            if t.get("matched") and (t.get("qobuz_id") or t.get("qobuz_album_id")):
                continue  # already matched
            if t.get("is_local"):
                continue
            key = f"{t.get('name', '').lower()}|{(t.get('artists') or [''])[0].lower()}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unmatched.append(t)

    log.info("Resolving qobuz IDs for %d unmatched tracks...", len(unmatched))

    resolved = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_search_one, t): t for t in unmatched}
        for i, fut in enumerate(as_completed(futures), 1):
            track, result = fut.result()
            if result:
                track["qobuz_id"] = str(result.get("id", ""))
                track["qobuz_album_id"] = str(result.get("album", {}).get("id", ""))
                resolved += 1
            if i % 100 == 0:
                log.info("  ... %d / %d done (%d resolved)", i, len(unmatched), resolved)

    log.info("Resolved %d / %d tracks to qobuz IDs", resolved, len(unmatched))

    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)

    log.info("Updated manifest written to %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
