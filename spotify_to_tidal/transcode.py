"""On-the-fly transcoding of lossless audio files to MP3 using FFmpeg.

Typical usage::

    from pathlib import Path
    from spotify_to_tidal.transcode import transcode_directory

    ok, fail = transcode_directory(Path("~/Music/tidal").expanduser())
    print(f"Transcoded {ok} files, {fail} failures")

Apple Silicon note: ``libmp3lame`` is already highly optimised on Apple
Silicon via NEON SIMD; there are no dedicated hardware MP3 encoder blocks on
M-series chips, so passing ``-c:a libmp3lame -q:a 0`` (V0 VBR, ~245 kbps) is
both the highest-quality and the fastest available option on that platform.
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _which_ffmpeg() -> str:
    """Return the absolute path to ffmpeg or raise :exc:`FileNotFoundError`."""
    path = shutil.which("ffmpeg")
    if not path:
        raise FileNotFoundError(
            "`ffmpeg` not found on PATH. Install it with `brew install ffmpeg` "
            "(macOS) or your system package manager, then ensure it is on PATH."
        )
    return path


def _is_apple_silicon() -> bool:
    """Return True when running on an Apple Silicon (arm64) Mac."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _build_ffmpeg_cmd(src: Path, dst: Path, ffmpeg_bin: str) -> list[str]:
    """Build the FFmpeg command list for *src* → *dst*.

    On Apple Silicon the VBR V0 preset (``-q:a 0``) gives the best quality/
    size ratio and is the recommended encoding mode for ``libmp3lame``.  On
    other platforms we fall back to CBR 320 kbps (``-b:a 320k``) for maximum
    compatibility with players that do not handle VBR well.
    """
    audio_flags: list[str]
    if _is_apple_silicon():
        # V0 VBR — best quality/size, well-optimised via NEON on Apple Silicon
        audio_flags = ["-c:a", "libmp3lame", "-q:a", "0"]
    else:
        # CBR 320 kbps — universally compatible fallback
        audio_flags = ["-c:a", "libmp3lame", "-b:a", "320k"]

    return [
        ffmpeg_bin,
        "-i", str(src),
        *audio_flags,
        "-map_metadata", "0",   # copy all tags from input
        "-id3v2_version", "3",  # widest player support
        "-y",                   # overwrite dst if it exists (guard skips before call)
        str(dst),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transcode_file(
    src: Path,
    dst: Path,
    *,
    delete_source: bool = False,
) -> bool:
    """Transcode a single lossless audio file to MP3.

    Parameters
    ----------
    src:
        Source audio file (e.g. a ``.flac``).
    dst:
        Desired output path (must end in ``.mp3``).
    delete_source:
        If *True* and the transcode succeeds, the original *src* file is
        removed after *dst* is written.  Defaults to *False*.

    Returns
    -------
    bool
        *True* on success, *False* on any FFmpeg error.

    Raises
    ------
    FileNotFoundError
        If ``ffmpeg`` is not found on ``PATH``.
    """
    ffmpeg_bin = _which_ffmpeg()

    if not src.is_file():
        log.warning("transcode_file: source does not exist or is not a file: %s", src)
        return False

    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = _build_ffmpeg_cmd(src, dst, ffmpeg_bin)
    log.debug("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="replace").strip()
        log.error(
            "FFmpeg failed (rc=%d) for %s:\n%s",
            result.returncode,
            src,
            stderr_text,
        )
        return False

    log.info("Transcoded: %s → %s", src, dst)

    if delete_source:
        try:
            src.unlink()
            log.debug("Deleted source: %s", src)
        except OSError as exc:
            log.warning("Could not delete source %s: %s", src, exc)

    return True


def transcode_directory(
    root: Path,
    *,
    delete_source: bool = False,
    pattern: str = "*.flac",
) -> tuple[int, int]:
    """Transcode all matching lossless files under *root* to MP3.

    Files are processed sequentially to avoid overwhelming the system.
    A file is skipped when a sibling ``.mp3`` with a *newer* modification
    time already exists next to the source — this makes re-runs idempotent.

    Parameters
    ----------
    root:
        Directory tree to search.
    delete_source:
        Passed through to :func:`transcode_file`; removes originals on
        success when *True*.  Defaults to *False*.
    pattern:
        Glob pattern used to find source files.  Defaults to ``"*.flac"``.
        Pass ``"*.{flac,alac,wav,aif,aiff}"`` (shell brace expansion is **not**
        supported; use separate calls) or e.g. ``"**/*.wav"`` for other
        lossless formats.

    Returns
    -------
    tuple[int, int]
        ``(success_count, fail_count)`` — number of files successfully
        transcoded and number that failed (skipped files count as neither).

    Raises
    ------
    FileNotFoundError
        If ``ffmpeg`` is not found on ``PATH`` (raised before any file is
        processed so the caller can bail early).
    """
    # Validate ffmpeg availability upfront — fail fast before iterating.
    _which_ffmpeg()

    if not root.is_dir():
        raise NotADirectoryError(f"transcode_directory: {root} is not a directory")

    sources = sorted(root.rglob(pattern))
    if not sources:
        log.info("transcode_directory: no files matching %r under %s", pattern, root)
        return 0, 0

    success = 0
    fail = 0

    for src in sources:
        dst = src.with_suffix(".mp3")

        # Skip if a newer mp3 sibling already exists.
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            log.debug("Skipping (up-to-date mp3 exists): %s", src)
            continue

        try:
            ok = transcode_file(src, dst, delete_source=delete_source)
        except Exception as exc:  # noqa: BLE001 — per-file errors must not abort batch
            log.error("Unexpected error transcoding %s: %s", src, exc)
            ok = False

        if ok:
            success += 1
        else:
            fail += 1

    log.info(
        "transcode_directory finished: %d transcoded, %d failed (root=%s)",
        success,
        fail,
        root,
    )
    return success, fail
