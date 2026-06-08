"""Factory for resolving downloader names to backend instances."""
from __future__ import annotations

from . import DownloaderBackend
from .tiddl import TiddlDownloader
from .tidarr import TidarrDownloader
from .qobuz import QobuzDownloader

_BACKENDS: dict[str, type[DownloaderBackend]] = {
    "tiddl": TiddlDownloader,
    "tidarr": TidarrDownloader,
    "qobuz": QobuzDownloader,
}

# Canonical expansion lists for virtual aliases
_ALL_BACKENDS: list[str] = ["tiddl", "tidarr", "qobuz"]
_BOTH_BACKENDS: list[str] = ["tiddl", "tidarr"]


def list_backends() -> list[str]:
    return list(_BACKENDS.keys())


def get_downloader_names(name: str) -> list[str]:
    """Expand a virtual or literal downloader name to a list of backend names.

    - ``"all"``  → ``["tiddl", "tidarr", "qobuz"]``
    - ``"both"`` → ``["tiddl", "tidarr"]``
    - any other string → ``[name]``
    """
    if name == "all":
        return list(_ALL_BACKENDS)
    if name == "both":
        return list(_BOTH_BACKENDS)
    return [name]


def get_downloader(name: str) -> DownloaderBackend:
    """Return a fresh backend instance by name.

    Raises :class:`ValueError` if *name* is unknown.
    """
    try:
        cls = _BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"Unknown downloader {name!r}. "
            f"Choose from: {', '.join(_BACKENDS)}"
        ) from None
    return cls()
