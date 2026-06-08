"""qobuz-dl CLI backend.

Drives the `qobuz-dl` command-line tool to download every matched item
in the manifest. One invocation per URL; qobuz-dl accepts a bare URL as
its sole argument.
"""
from __future__ import annotations

import collections
import shutil
import subprocess
from typing import Any

from ..config import AppConfig
from ..manifest import Manifest
from . import DownloaderBackend

_QOBUZ_ALBUM_BASE = "https://play.qobuz.com/album"
_QOBUZ_TRACK_BASE = "https://play.qobuz.com/track"


def _build_qobuz_url_list(manifest: Manifest) -> list[tuple[str, str]]:
    """Return [(url, type), ...] for all Qobuz-matched items.

    Playlist grouping rule (mirrors tiddl backend):
      - If ≥2 matched tracks in a playlist share the same qobuz_album_id,
        emit one album URL for that album (skip individual track URLs).
      - Otherwise emit individual track URLs.

    Artist albums always emit album URLs.
    """
    items: list[tuple[str, str]] = []

    # Playlists — group by qobuz_album_id.
    for pl in manifest.playlists:
        album_track_count: dict[str, int] = collections.defaultdict(int)
        for t in pl.tracks:
            qid = getattr(t, "qobuz_id", None)
            qaid = getattr(t, "qobuz_album_id", None)
            if t.matched and qid and qaid:
                album_track_count[qaid] += 1

        emitted_albums: set[str] = set()
        for t in pl.tracks:
            qid = getattr(t, "qobuz_id", None)
            if not (t.matched and qid):
                continue
            qaid = getattr(t, "qobuz_album_id", None)
            if qaid and album_track_count[qaid] >= 2:
                if qaid not in emitted_albums:
                    items.append((f"{_QOBUZ_ALBUM_BASE}/{qaid}", "album"))
                    emitted_albums.add(qaid)
            else:
                items.append((f"{_QOBUZ_TRACK_BASE}/{qid}", "track"))

    # Artist albums — always album URLs.
    for a in manifest.artists:
        if not a.matched:
            continue
        for al in a.albums:
            qid = getattr(al, "qobuz_id", None)
            if al.matched and qid:
                items.append((f"{_QOBUZ_ALBUM_BASE}/{qid}", "album"))

    return items


class QobuzDownloader(DownloaderBackend):
    @property
    def name(self) -> str:
        return "qobuz"

    def download(self, manifest: Manifest, cfg: AppConfig, **_kwargs: Any) -> int:
        """Download every Qobuz-matched item using `qobuz-dl`.

        Returns 0 on full success, or the last non-zero exit code on failure.
        """
        binary = shutil.which("qobuz-dl")
        if binary is None:
            print(
                "[qobuz] ERROR: `qobuz-dl` not found on PATH. "
                "Install it with `pip install qobuz-dl` and authenticate."
            )
            return 1

        url_list = _build_qobuz_url_list(manifest)
        if not url_list:
            print("[qobuz] Nothing to download.")
            return 0

        output_dir = str(cfg.tidal_download_dir)
        print(f"[qobuz] Downloading {len(url_list)} item(s) → {output_dir}")

        last_rc = 0
        for url, kind in url_list:
            print(f"[qobuz] {kind}: {url}")
            try:
                result = subprocess.run(
                    [binary, url],
                    cwd=output_dir,
                    timeout=300.0,
                )
            except subprocess.TimeoutExpired:
                print(f"[qobuz] TIMEOUT (300s): {url}")
                last_rc = 1
                continue
            if result.returncode != 0:
                print(f"[qobuz] FAILED ({result.returncode}): {url}")
                last_rc = result.returncode

        return last_rc
