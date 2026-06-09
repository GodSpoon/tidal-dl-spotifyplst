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
import logging
import os
import platform
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _which_ffmpeg() -> str:
    """Return the absolute path to ffmpeg or raise :exc:`FileNotFoundError`."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise FileNotFoundError(
            "ffmpeg not found on PATH. Install it via Homebrew: brew install ffmpeg"
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
        Destination path (should end in ``.mp3``).
    delete_source:
        When *True*, the original *src* is deleted after a successful
        transcode.  Defaults to *False*.

    Returns
    -------
    bool
        *True* on success, *False* on failure.
    """
    ffmpeg = _which_ffmpeg()
    cmd = _build_ffmpeg_cmd(src, dst, ffmpeg)

    log.info("Transcoding %s → %s", src, dst)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        log.error("FFmpeg failed for %s: %s", src, stderr.strip())
        # Clean up partial output so re-runs don't see it as "up-to-date".
        if dst.exists():
            dst.unlink()
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error transcoding %s: %s", src, exc)
        if dst.exists():
            dst.unlink()
        return False

    if delete_source:
        try:
            src.unlink()
        except OSError as exc:
            log.warning("Could not delete source %s: %s", src, exc)

    return True


def _transcode_one(
    src: Path,
    *,
    delete_source: bool = False,
) -> bool | None:
    """Skip-check + transcode helper for parallel execution.

    Returns *True* on success, *False* on failure, *None* when skipped.
    """
    dst = src.with_suffix(".mp3")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        log.debug("Skipping (up-to-date mp3 exists): %s", src)
        return None
    try:
        return transcode_file(src, dst, delete_source=delete_source)
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error transcoding %s: %s", src, exc)
        return False


def transcode_directory(
    root: Path,
    *,
    delete_source: bool = False,
    pattern: str = "*.flac",
    workers: int = 1,
) -> tuple[int, int]:
    """Transcode all matching lossless files under *root* to MP3.

    Files are processed in parallel when *workers* > 1.  A file is skipped
    when a sibling ``.mp3`` with a *newer* modification time already exists
    next to the source — this makes re-runs idempotent.

    Parameters
    ----------
    root:
        Directory tree to search.
    delete_source:
        Passed through to :func:`transcode_file`; removes originals on
        success when *True*.  Defaults to *False*.
    pattern:
        Glob pattern used to find source files.  Defaults to ``"*.flac"``.
    workers:
        Number of parallel worker threads.  ``1`` (default) processes files
        sequentially.  ``0`` auto-selects ``os.cpu_count()``.  Each worker
        invokes FFmpeg as a subprocess, so the actual parallelism is limited
        by CPU cores and I/O bandwidth.

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

    if workers == 0:
        workers = os.cpu_count() or 1

    if not root.is_dir():
        raise NotADirectoryError(f"transcode_directory: {root} is not a directory")

    sources = sorted(root.rglob(pattern))
    if not sources:
        log.info("transcode_directory: no files matching %r under %s", pattern, root)
        return 0, 0

    total = len(sources)
    print(f"[i] Found {total} file(s) to transcode (workers={workers}) …")

    if workers <= 1:
        results = [_transcode_one(src, delete_source=delete_source) for src in sources]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(
                executor.map(
                    lambda s: _transcode_one(s, delete_source=delete_source),
                    sources,
                )
            )

    success = sum(1 for r in results if r is True)
    fail = sum(1 for r in results if r is False)
    skipped = total - success - fail

    log.info(
        "transcode_directory finished: %d transcoded, %d failed, %d skipped (root=%s)",
        success,
        fail,
        skipped,
        root,
    )
    return success, fail


def _mirror_one(
    src: Path,
    src_root: Path,
    dst_root: Path,
    *,
    delete_source: bool = False,
) -> bool | None:
    """Skip-check + transcode helper that mirrors directory structure.

    Returns *True* on success, *False* on failure, *None* when skipped.
    """
    rel = src.relative_to(src_root)
    dst = (dst_root / rel).with_suffix(".mp3")

    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        log.debug("Skipping (up-to-date mp3 exists): %s", src)
        return None

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        return transcode_file(src, dst, delete_source=delete_source)
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error transcoding %s: %s", src, exc)
        return False


def transcode_mirror(
    src_root: Path,
    dst_root: Path,
    *,
    delete_source: bool = False,
    pattern: str = "*.flac",
    workers: int = 1,
) -> tuple[int, int, int]:
    """Mirror-transcode all matching lossless files from *src_root* to *dst_root*.

    Directory structure is preserved: a FLAC at
    ``src_root/Artist/Album/track.flac`` becomes
    ``dst_root/Artist/Album/track.mp3``.

    Files are processed in parallel when *workers* > 1.  A file is skipped
    when a corresponding ``.mp3`` in the mirror tree with a *newer*
    modification time already exists — this makes re-runs idempotent.

    Parameters
    ----------
    src_root:
        Source directory tree (e.g., the FLAC library).
    dst_root:
        Destination directory tree (e.g., the MP3 library).
    delete_source:
        Passed through to :func:`transcode_file`.  Defaults to *False*.
    pattern:
        Glob pattern used to find source files.  Defaults to ``"*.flac"``.
    workers:
        Number of parallel worker threads.  ``1`` (default) processes files
        sequentially.  ``0`` auto-selects ``os.cpu_count()``.  Each worker
        invokes FFmpeg as a subprocess.

    Returns
    -------
    tuple[int, int, int]
        ``(success_count, fail_count, skipped_count)``.

    Raises
    ------
    FileNotFoundError
        If ``ffmpeg`` is not found on ``PATH``.
    NotADirectoryError
        If *src_root* is not a directory.
    """
    _which_ffmpeg()

    if workers == 0:
        workers = os.cpu_count() or 1

    src_root = src_root.expanduser().resolve()
    dst_root = dst_root.expanduser().resolve()

    if not src_root.is_dir():
        raise NotADirectoryError(f"transcode_mirror: {src_root} is not a directory")

    sources = sorted(src_root.rglob(pattern))
    if not sources:
        log.info("transcode_mirror: no files matching %r under %s", pattern, src_root)
        return 0, 0, 0

    total = len(sources)
    print(f"[i] Found {total} file(s) to mirror-transcode (workers={workers})")
    print(f"    src: {src_root}")
    print(f"    dst: {dst_root}")

    if workers <= 1:
        results = [
            _mirror_one(src, src_root, dst_root, delete_source=delete_source)
            for src in sources
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(
                executor.map(
                    lambda s: _mirror_one(
                        s, src_root, dst_root, delete_source=delete_source
                    ),
                    sources,
                )
            )

    success = sum(1 for r in results if r is True)
    fail = sum(1 for r in results if r is False)
    skipped = total - success - fail

    print(
        f"[OK] Mirror-transcode finished: {success} transcoded, "
        f"{fail} failed, {skipped} skipped."
    )
    return success, fail, skipped