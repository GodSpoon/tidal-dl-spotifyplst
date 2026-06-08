"""Organize downloaded audio files into a structured library directory.

Reads metadata from embedded tags (ID3 / Vorbis Comments via ``mutagen``)
and falls back to manifest data or filename heuristics when tags are absent.

Typical usage
-------------
::

    from spotify_to_tidal.organizer import organize_from_manifest, OrganizeConfig
    from pathlib import Path

    results = organize_from_manifest(
        manifest,
        cfg,
        library_dir=Path("/Volumes/media/Audio/Music"),
        schema="{artist}/{year} - {album}/{track_num:02d} - {title}.{ext}",
    )
    for r in results:
        print(r.action, r.source.name, "→", r.destination)
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig
from .manifest import Manifest, TrackEntry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional mutagen import
# ---------------------------------------------------------------------------

try:
    from mutagen import File as _MutagenFile  # type: ignore[import]

    _MUTAGEN_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MutagenFile = None  # type: ignore[assignment, misc]
    _MUTAGEN_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default path schema (relative to ``library_dir``).
DEFAULT_SCHEMA: str = "{artist}/{album}/{track_num:02d} - {title}.{ext}"

#: File extensions treated as audio content.
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aac", ".opus"}
)

# Characters illegal on macOS *or* Windows (union set).
# macOS forbids ``/`` and NUL; Windows forbids ``<>:"/\|?*`` + control chars.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Collapse runs of repeated whitespace/underscores left after sanitising.
_WHITESPACE_RE = re.compile(r"[ _]{2,}")

# Matches a common ripper-prefix such as "01 - ", "1. ", "03 ".
_TRACK_PREFIX_RE = re.compile(r"^(\d{1,3})[.\-\s]+(.+)$")

# Tidal track IDs are typically 6–12 digits; used to match filenames back to
# manifest entries when embedded tags are absent.
_TIDAL_ID_RE = re.compile(r"\b(\d{6,12})\b")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class TrackMeta:
    """Normalised metadata for a single audio file.

    All string fields are *raw* (not yet sanitised for the filesystem).
    ``track_num`` defaults to 0 when unknown so ``{track_num:02d}`` still
    formats cleanly (outputs ``"00"``).
    """

    title: str = "Unknown Title"
    artist: str = "Unknown Artist"
    album: str = "Unknown Album"
    year: str = ""
    track_num: int = 0
    disc_num: int = 1


@dataclass
class OrganizeConfig:
    """Parameters that govern a single organize run.

    Attributes
    ----------
    library_dir:
        Root of the target music library.
    schema:
        ``str.format_map``-compatible template whose placeholders map to
        :class:`TrackMeta` attributes plus ``{ext}`` (extension *without*
        the leading dot).  The result is treated as a relative path from
        ``library_dir``.

        Built-in placeholders: ``{artist}``, ``{album}``, ``{title}``,
        ``{year}``, ``{track_num}``, ``{disc_num}``, ``{ext}``.

        Format specs are supported: ``{track_num:02d}`` zero-pads to two
        digits.  String placeholders are sanitised for the filesystem
        automatically.

        Default: ``"{artist}/{album}/{track_num:02d} - {title}.{ext}"``
    copy:
        When *True*, copy files rather than moving them.  Default is to
        move (destructive but avoids duplicate storage).
    """

    library_dir: Path
    schema: str = DEFAULT_SCHEMA
    copy: bool = False


@dataclass
class OrganizeResult:
    """Outcome of organizing a single file.

    Attributes
    ----------
    source:
        Original file path.
    destination:
        Where the file ended up, or *None* on error.
    action:
        One of ``"moved"``, ``"copied"``, ``"skipped"``, ``"error"``.
    reason:
        Human-readable explanation (populated for ``"skipped"`` and
        ``"error"`` actions).
    """

    source: Path
    destination: Optional[Path]
    action: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------


def sanitize_part(name: str) -> str:
    """Strip or replace characters that are illegal on macOS or Windows.

    Leading/trailing dots and spaces are removed (Windows treats them as
    special).  Returns at least ``"_"`` to avoid empty path components.

    Parameters
    ----------
    name:
        A single path component (artist, album, title, …).

    Returns
    -------
    str
        The sanitised component, always non-empty.
    """
    name = _ILLEGAL_RE.sub("_", name)
    name = _WHITESPACE_RE.sub(" ", name)
    name = name.strip(". ")
    return name or "_"


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def _meta_from_mutagen(path: Path) -> Optional[TrackMeta]:
    """Return :class:`TrackMeta` read via *mutagen*, or *None* on failure.

    Uses the *easy=True* interface so tag names are normalised across
    formats (FLAC Vorbis comments, MP3 ID3, AAC, …).
    """
    if not _MUTAGEN_AVAILABLE:
        return None
    try:
        mf = _MutagenFile(str(path), easy=True)
    except Exception:  # noqa: BLE001 – mutagen raises many different errors
        return None
    if mf is None:
        return None

    def _first(key: str) -> str:
        vals: Any = mf.get(key) or []
        return str(vals[0]).strip() if vals else ""

    title = _first("title")
    if not title:
        # Without a title tag the file is essentially unidentifiable; let
        # callers fall through to manifest/filename heuristics.
        return None

    artist = _first("artist") or _first("albumartist") or "Unknown Artist"
    album = _first("album") or "Unknown Album"

    date = _first("date")
    year = date[:4] if date else ""

    raw_track = _first("tracknumber")  # may be "3" or "3/12"
    try:
        track_num = int(raw_track.split("/")[0])
    except (ValueError, AttributeError):
        track_num = 0

    raw_disc = _first("discnumber")  # may be "1" or "1/2"
    try:
        disc_num = int(raw_disc.split("/")[0])
    except (ValueError, AttributeError):
        disc_num = 1

    return TrackMeta(
        title=title,
        artist=artist,
        album=album,
        year=year,
        track_num=track_num,
        disc_num=disc_num,
    )


def _meta_from_entry(entry: TrackEntry) -> TrackMeta:
    """Build :class:`TrackMeta` from a matched manifest :class:`TrackEntry`.

    Prefers Tidal-side data (what was actually downloaded) and falls back
    to the Spotify side.
    """
    title = entry.tidal_title or entry.name or "Unknown Title"
    artist = entry.tidal_artist or (
        entry.artists[0] if entry.artists else "Unknown Artist"
    )
    album = entry.tidal_album or entry.album or "Unknown Album"
    return TrackMeta(title=title, artist=artist, album=album)


def _meta_from_filename(path: Path) -> TrackMeta:
    """Best-effort :class:`TrackMeta` parsed from *path*.

    Assumes a layout such as ``<artist>/<album>/01 - Title.flac`` but
    degrades gracefully to just the stem when parent directories are
    uninformative (e.g. ``.`` or the filesystem root).
    """
    stem = path.stem
    track_num = 0

    m = _TRACK_PREFIX_RE.match(stem)
    if m:
        track_num = int(m.group(1))
        title = m.group(2).strip()
    else:
        title = stem

    root = Path(path.root)
    parent = path.parent
    grandparent = parent.parent

    album = parent.name if parent != root and parent.name else "Unknown Album"
    artist = (
        grandparent.name
        if grandparent not in (parent, root) and grandparent.name
        else "Unknown Artist"
    )

    return TrackMeta(title=title, artist=artist, album=album, track_num=track_num)


def read_metadata(path: Path, hint: Optional[TrackEntry] = None) -> TrackMeta:
    """Return the best available :class:`TrackMeta` for *path*.

    Resolution order:

    1. Embedded tags via *mutagen* (most accurate).
    2. Manifest *hint* (a matched :class:`TrackEntry`).
    3. Filename / directory-name heuristics (last resort).

    Parameters
    ----------
    path:
        Absolute path to the audio file.
    hint:
        Optional manifest entry used as a fallback when tags are absent.
    """
    meta = _meta_from_mutagen(path)
    if meta is not None:
        return meta
    if hint is not None:
        return _meta_from_entry(hint)
    return _meta_from_filename(path)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class _SchemaFields(dict):  # type: ignore[type-arg]
    """``dict`` subclass that sanitises string values on lookup.

    Used with ``str.format_map`` so every ``{field}`` placeholder is
    filesystem-safe without a separate pass.  Numeric values (``int``,
    ``float``) are returned unchanged so format specs like
    ``{track_num:02d}`` continue to work.
    """

    def __missing__(self, key: str) -> str:
        # Return the placeholder itself for unknown keys rather than raising.
        return f"{{{key}}}"

    def __getitem__(self, key: str) -> Any:  # type: ignore[override]
        val = super().__getitem__(key)
        if isinstance(val, str):
            return sanitize_part(val)
        return val


def resolve_destination(meta: TrackMeta, ext: str, cfg: OrganizeConfig) -> Path:
    """Apply *cfg.schema* to *meta* and return the absolute destination path.

    Parameters
    ----------
    meta:
        Track metadata supplying template values.
    ext:
        File extension **without** the leading dot (e.g. ``"flac"``).
    cfg:
        Organize configuration supplying *library_dir* and *schema*.
    """
    fields = _SchemaFields(
        title=meta.title,
        artist=meta.artist,
        album=meta.album,
        year=meta.year or "0000",
        track_num=meta.track_num,
        disc_num=meta.disc_num,
        ext=sanitize_part(ext),
    )
    relative = cfg.schema.format_map(fields)
    # Guard against a schema that accidentally produces an absolute path.
    relative = relative.lstrip("/\\")
    return cfg.library_dir / relative


# ---------------------------------------------------------------------------
# Collision handling
# ---------------------------------------------------------------------------


def _unique_destination(dest: Path, source: Path) -> tuple[Path, bool]:
    """Return *(final_dest, skip)*, resolving filename collisions.

    Rules:

    * If *dest* does not exist → *(dest, False)* (proceed normally).
    * If *dest* exists with the **same size** as *source* → *(dest, True)*
      (identical file already present; skip).
    * If *dest* exists with a **different size** → append ``" (N)"`` before
      the suffix (incrementing *N*) until a free or same-size slot is found.
    """
    if not dest.exists():
        return dest, False

    source_size = source.stat().st_size
    if dest.stat().st_size == source_size:
        return dest, True  # already there, same size

    stem, suffix = dest.stem, dest.suffix
    parent = dest.parent
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate, False
        if candidate.stat().st_size == source_size:
            return candidate, True
        n += 1


# ---------------------------------------------------------------------------
# Core organize functions
# ---------------------------------------------------------------------------


def organize_file(
    source: Path,
    cfg: OrganizeConfig,
    hint: Optional[TrackEntry] = None,
) -> OrganizeResult:
    """Move or copy *source* into the library according to *cfg*.

    Parameters
    ----------
    source:
        Absolute path to the audio file.
    cfg:
        Destination library configuration.
    hint:
        Optional manifest entry used as a metadata fallback when embedded
        tags are missing.

    Returns
    -------
    OrganizeResult
        Describes what happened: ``action`` is one of ``"moved"``,
        ``"copied"``, ``"skipped"``, or ``"error"``.
    """
    ext = source.suffix.lstrip(".")
    meta = read_metadata(source, hint)
    dest = resolve_destination(meta, ext, cfg)
    final_dest, skip = _unique_destination(dest, source)

    if skip:
        log.debug("skip %s → already exists at %s", source.name, final_dest)
        return OrganizeResult(
            source=source,
            destination=final_dest,
            action="skipped",
            reason="same size already present",
        )

    try:
        final_dest.parent.mkdir(parents=True, exist_ok=True)
        if cfg.copy:
            shutil.copy2(source, final_dest)
            action = "copied"
        else:
            shutil.move(str(source), final_dest)
            action = "moved"
    except OSError as exc:
        log.error("Failed to organize %s: %s", source, exc)
        return OrganizeResult(
            source=source, destination=None, action="error", reason=str(exc)
        )

    log.info("%s %s → %s", action, source.name, final_dest)
    return OrganizeResult(source=source, destination=final_dest, action=action)


def organize_directory(
    source_dir: Path,
    cfg: OrganizeConfig,
) -> list[OrganizeResult]:
    """Recursively organize all audio files under *source_dir*.

    Useful when no manifest is available; metadata comes entirely from
    embedded tags or filename/directory heuristics.

    Parameters
    ----------
    source_dir:
        Root of the raw download tree to scan.
    cfg:
        Destination library and schema configuration.

    Returns
    -------
    list[OrganizeResult]
        One entry per audio file found under *source_dir*.
    """
    results: list[OrganizeResult] = []
    if not source_dir.is_dir():
        log.warning(
            "source_dir %s does not exist or is not a directory", source_dir
        )
        return results

    for path in sorted(source_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            results.append(organize_file(path, cfg))

    return results


# ---------------------------------------------------------------------------
# Manifest-guided organization
# ---------------------------------------------------------------------------


def _build_tidal_id_index(manifest: Manifest) -> dict[int, TrackEntry]:
    """Return a ``{tidal_id: TrackEntry}`` index built from all playlist tracks.

    Only matched entries (``track.matched is True`` and ``track.tidal_id``
    is set) are included.
    """
    index: dict[int, TrackEntry] = {}
    for playlist in manifest.playlists:
        for track in playlist.tracks:
            if track.matched and track.tidal_id is not None:
                index[track.tidal_id] = track
    return index


def _guess_tidal_id_from_path(path: Path) -> Optional[int]:
    """Try to extract a numeric Tidal track ID from *path*.

    *tiddl* sometimes embeds the Tidal ID in the filename or an enclosing
    directory name when downloading by URL.  Checks path components from
    most-specific (filename) to least-specific (parent directories).
    This is a best-effort heuristic and may return false positives.
    """
    for part in reversed(path.parts):
        m = _TIDAL_ID_RE.search(part)
        if m:
            return int(m.group(1))
    return None


def organize_from_manifest(
    manifest: Manifest,
    cfg: AppConfig,
    library_dir: Path,
    *,
    schema: str = DEFAULT_SCHEMA,
    copy: bool = False,
) -> list[OrganizeResult]:
    """Organize downloaded files guided by matched manifest entries.

    Uses :attr:`~config.AppConfig.tidal_download_dir` as the source tree.
    For each audio file found the metadata resolution order is:

    1. Embedded tags via *mutagen*.
    2. Manifest :class:`~manifest.TrackEntry` matched by Tidal ID heuristic.
    3. Filename / directory-name parsing.

    Parameters
    ----------
    manifest:
        Populated manifest (post-match stage, so Tidal IDs are filled in).
    cfg:
        Application configuration; provides ``tidal_download_dir``.
    library_dir:
        Root of the target music library
        (e.g. ``Path("/Volumes/media/Audio/Music")``).
    schema:
        Path template string.  Defaults to :data:`DEFAULT_SCHEMA`.
        Example with year: ``"{artist}/{year} - {album}/{track_num:02d} - {title}.{ext}"``
    copy:
        When *True*, copy files; otherwise move them (default).

    Returns
    -------
    list[OrganizeResult]
        One result per audio file processed under ``tidal_download_dir``.
    """
    org_cfg = OrganizeConfig(library_dir=library_dir, schema=schema, copy=copy)
    id_index = _build_tidal_id_index(manifest)
    source_dir = cfg.tidal_download_dir

    results: list[OrganizeResult] = []
    if not source_dir.is_dir():
        log.warning(
            "tidal_download_dir %s does not exist or is not a directory; "
            "nothing to organize",
            source_dir,
        )
        return results

    for path in sorted(source_dir.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS):
            continue

        hint: Optional[TrackEntry] = None
        tidal_id = _guess_tidal_id_from_path(path)
        if tidal_id is not None:
            hint = id_index.get(tidal_id)

        results.append(organize_file(path, org_cfg, hint=hint))

    errors = sum(1 for r in results if r.action == "error")
    log.info(
        "organize_from_manifest: %d files processed, %d errors",
        len(results),
        errors,
    )
    return results
