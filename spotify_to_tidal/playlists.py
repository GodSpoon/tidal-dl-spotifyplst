"""Generate M3U8 playlists from a synced Manifest.

Each PlaylistEntry in the manifest becomes one ``.m3u8`` file whose
entries point (with relative paths) at the audio files that tiddl placed
under *library_dir*.  Only matched tracks (those with a ``tidal_id``) are
included.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .manifest import Manifest, TrackEntry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".m4a", ".mp3", ".ogg", ".wav", ".aac", ".opus"}
)

_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
_NORMALIZE_RE = re.compile(r"[^a-z0-9]")


def _sanitize_filename(name: str) -> str:
    """Replace filesystem-unsafe characters and trim whitespace."""
    return _UNSAFE_CHARS_RE.sub("_", name).strip()


def _normalize(s: str) -> str:
    """Collapse to lowercase alphanumerics only — used for fuzzy matching."""
    return _NORMALIZE_RE.sub("", s.lower())


def _build_audio_index(library_dir: Path) -> dict[str, Path]:
    """Scan *library_dir* recursively and return {normalised_stem -> path}."""
    index: dict[str, Path] = {}
    for f in library_dir.rglob("*"):
        if f.suffix.lower() in _AUDIO_EXTENSIONS:
            index[_normalize(f.stem)] = f
    return index


def _find_audio_file(
    index: dict[str, Path], track: TrackEntry
) -> Optional[Path]:
    """Return the best-matching audio file for *track*, or ``None``."""
    # Try each candidate title in decreasing specificity.
    candidates = [
        track.tidal_title or "",
        track.name,
    ]
    for title in candidates:
        if not title:
            continue
        key = _normalize(title)
        if key in index:
            return index[key]
        # Substring match: the file stem may include track number prefix.
        for stem_key, path in index.items():
            if key and key in stem_key:
                return path
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_m3u_playlists(manifest: Manifest, library_dir: Path) -> list[Path]:
    """Generate one ``.m3u8`` per playlist entry and return the written paths.

    Files are written alongside *library_dir* in a ``playlists/`` sub-directory
    unless an explicit *output_dir* is passed to :func:`write_playlists`.
    """
    return write_playlists(manifest, library_dir)


def write_playlists(
    manifest: Manifest,
    library_dir: Path,
    output_dir: Optional[Path] = None,
) -> list[Path]:
    """Write M3U8 playlist files and return the list of written paths.

    Args:
        manifest: The synced manifest containing playlist and track data.
        library_dir: Root directory where tiddl placed the downloaded audio.
        output_dir: Directory to write ``.m3u8`` files into.  Defaults to
            ``library_dir / "playlists"``.

    Returns:
        List of :class:`~pathlib.Path` objects for every file written.
    """
    if output_dir is None:
        output_dir = library_dir / "playlists"
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_index = _build_audio_index(library_dir)
    written: list[Path] = []

    for pl in manifest.playlists:
        safe_name = _sanitize_filename(pl.name) or f"playlist_{pl.spotify_id}"
        out_path = output_dir / f"{safe_name}.m3u8"

        lines: list[str] = ["#EXTM3U", f"# {pl.name}", ""]

        for t in pl.tracks:
            if not (t.matched and t.tidal_id):
                continue

            audio_path = _find_audio_file(audio_index, t)
            if audio_path is None:
                # Track matched in Tidal but file not on disk yet — skip.
                lines.append(f"# missing: {t.tidal_title or t.name}")
                continue

            duration_s: int = t.tidal_duration if t.tidal_duration is not None else -1
            title = t.tidal_title or t.name
            artist = t.tidal_artist or (t.artists[0] if t.artists else "")
            display = f"{artist} - {title}" if artist else title

            # Relative path from the playlist file to the audio file.
            try:
                rel = audio_path.relative_to(output_dir)
            except ValueError:
                # audio_path is outside output_dir; use an absolute path instead.
                rel = audio_path

            lines.append(f"#EXTINF:{duration_s},{display}")
            lines.append(str(rel))

        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(out_path)

    return written
