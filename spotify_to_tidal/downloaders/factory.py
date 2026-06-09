"""Factory for resolving downloader names to backend instances."""
from __future__ import annotations

from . import DownloaderBackend
from .tiddl import TiddlDownloader
from .qobuz import QobuzDownloader
from .squidwtf import SquidWtfDownloader

_BACKENDS: dict[str, type[DownloaderBackend]] = {
    "tiddl": TiddlDownloader,
    "qobuz": QobuzDownloader,
    "squidwtf": SquidWtfDownloader,
}


def list_backends() -> list[str]:
    return list(_BACKENDS.keys())


def get_downloader_names(name: str) -> list[str]:
    """Expand a virtual or literal downloader name to a list of backend names.

    - ``"all"``  → ``["tiddl", "qobuz", "squidwtf"]``
    - any other string → ``[name]``
    """
    if name == "all":
        return list(_BACKENDS.keys())
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
