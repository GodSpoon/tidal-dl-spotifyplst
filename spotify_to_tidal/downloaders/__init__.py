"""Modular downloader backends for spotify_to_tidal.

Backends implement the ``DownloaderBackend`` interface so the pipeline
can drive `tiddl`, `tidarr`, `qobuz-dl`, or any future tool uniformly.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
from abc import ABC, abstractmethod
from typing import Any

from ..config import AppConfig
from ..manifest import Manifest

log = logging.getLogger(__name__)


class DownloaderBackend(ABC):
    """Abstract base for a download backend."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier (e.g. ``tiddl``)."""

    @abstractmethod
    def download(self, manifest: Manifest, cfg: AppConfig, **kwargs: Any) -> int:
        """Download every matched item in *manifest*.

        Extra keyword arguments are backend-specific (e.g. chunk_size).

        Returns an exit code: ``0`` if everything succeeded, otherwise a
        non-zero value (the last error encountered).
        """


def run_backends_in_parallel(
    manifest: Manifest,
    cfg: AppConfig,
    names: list[str],
    **kwargs: Any,
) -> dict[str, int]:
    """Run each named backend concurrently in a thread pool.

    Creates a per-backend output subdirectory under ``cfg.tidal_download_dir``.
    Catches exceptions per backend, logs them, and records rc=1.
    Returns ``{name: rc}``.
    """
    if not names:
        return {}

    from . import factory as _factory  # lazy: avoids circular at module import time

    def _run(name: str) -> tuple[str, int]:
        try:
            backend = _factory.get_downloader(name)
            per_backend_cfg = dataclasses.replace(
                cfg, tidal_download_dir=cfg.tidal_download_dir / name
            )
            rc = backend.download(manifest, per_backend_cfg, **kwargs)
        except Exception as exc:
            log.error("Backend %r raised an exception: %s", name, exc, exc_info=True)
            rc = 1
        return name, rc

    max_workers = min(8, len(names))
    results: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_run, n): n for n in names}
        for fut in concurrent.futures.as_completed(futs):
            name, rc = fut.result()
            results[name] = rc
    return results
