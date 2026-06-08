"""Tiddl CLI backend (default).

Refactored from the original ``downloader.py`` to implement the
``DownloaderBackend`` interface.
"""
from __future__ import annotations

import collections
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import AppConfig
from ..manifest import Manifest
from . import DownloaderBackend

# tiddl prints rate-limit failures like:
#   "API Error: Response body does not contain valid json., 429/0 (track/424959206)"
# 429 = Tidal's per-IP HTTP rate limit; tiddl does not retry by itself.
# We use this pattern to detect a chunk that hit the limiter and re-run it
# after a backoff (already-downloaded files are skipped by tiddl's default
# --skip behavior, so re-running is essentially free for the successes).
_RATE_LIMIT_RE = re.compile(r"\b429/\d+")


def _which_tiddl() -> str:
    path = shutil.which("tiddl")
    if not path:
        raise FileNotFoundError(
            "`tiddl` not found on PATH. Install it with `pipx install tiddl` "
            "and run `tiddl auth login` once to authenticate."
        )
    return path


# Backwards-compatible alias — older code imported `_which_tidal_dl`.
_which_tidal_dl = _which_tiddl


def build_tidal_input_file(manifest: Manifest, out_path: Path) -> tuple[int, int]:
    """Write a tidal-dl -l input file. Returns (track_count, album_count)."""
    lines: list[str] = [
        f"# Generated {manifest.generated_at}",
        f"# Spotify user: {manifest.spotify_user_id}",
        "",
    ]
    track_n = 0
    album_n = 0

    # Playlists — group tracks by Tidal album; emit album URL when ≥2 tracks share one.
    for pl in manifest.playlists:
        lines.append(f"# === Playlist: {pl.name} ({pl.track_count} tracks) ===")
        # Count matched tracks per Tidal album.
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
                # Whole album is more efficient; emit album URL once.
                if aid not in emitted_albums:
                    lines.append(
                        f"# album: {t.tidal_album or ''} ({album_track_count[aid]} tracks)"
                    )
                    lines.append(f"https://tidal.com/browse/album/{aid}")
                    album_n += 1
                    emitted_albums.add(aid)
            else:
                lines.append(f"https://tidal.com/browse/track/{t.tidal_id}")
                track_n += 1
        lines.append("")

    # Artist albums
    for a in manifest.artists:
        if not a.matched:
            continue
        lines.append(f"# === Artist: {a.name} ===")
        for al in a.albums:
            if al.matched and al.tidal_id:
                lines.append(f"https://tidal.com/browse/album/{al.tidal_id}")
                album_n += 1
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return track_n, album_n


def _run_chunk_with_429_retry(
    cmd: list[str],
    *,
    chunk_index: int,
    total_chunks: int,
    chunk_size: int,
    max_429_retries: int,
    sleep_fn,
    chunk_timeout: float = 300.0,
) -> int:
    """Invoke `tiddl` for one chunk; retry on 429 with exponential backoff.

    The first attempt streams tiddl's output so the user sees per-track
    progress. On non-zero exit we do a single captured run to find a
    `429/N` line in tiddl's logs (so we don't false-positive on unrelated
    errors like a single track 404'ing). If 429 is detected we sleep
    `2 ** attempt` seconds (capped at 60) and re-run the same chunk;
    tiddl's default `--skip` makes the retry cheap because already-
    downloaded tracks are recognized and skipped.

    `chunk_timeout` (default 300s) prevents tiddl from hanging forever
    on a stuck track stream. If the timeout fires we kill the process
    and treat it as a non-retryable error so the pipeline continues.
    """
    attempt = 0
    while True:
        print(
            f"[i] Chunk {chunk_index}/{total_chunks} "
            f"({chunk_size} URLs)"
            + (f", attempt {attempt + 1}" if attempt else "")
        )
        try:
            result = subprocess.run(cmd, check=False, timeout=chunk_timeout)
        except subprocess.TimeoutExpired:
            print(
                f"[!] tiddl chunk {chunk_index} timed out after "
                f"{chunk_timeout}s (likely hung on a track stream); "
                f"continuing with next chunk"
            )
            return 1
        if result.returncode == 0:
            return 0
        if max_429_retries <= 0 or attempt >= max_429_retries:
            print(
                f"[!] tiddl chunk {chunk_index} exited with "
                f"{result.returncode}; continuing with next chunk"
            )
            return result.returncode
        # Re-run with captured output to confirm this is a 429 (not
        # e.g. a malformed URL or tiddl crash). Already-downloaded tracks
        # in the chunk are skipped by tiddl's default --skip behavior.
        try:
            result = subprocess.run(
                cmd, check=False, capture_output=True, text=True,
                timeout=chunk_timeout,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[!] tiddl chunk {chunk_index} timed out on retry after "
                f"{chunk_timeout}s; continuing with next chunk"
            )
            return 1
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode == 0:
            return 0
        if not _RATE_LIMIT_RE.search(combined):
            print(
                f"[!] tiddl chunk {chunk_index} exited with "
                f"{result.returncode} (no 429 detected); continuing"
            )
            return result.returncode
        attempt += 1
        backoff = min(60, 2 ** attempt)
        print(
            f"[!] tiddl chunk {chunk_index} hit 429 "
            f"(attempt {attempt}/{max_429_retries}); "
            f"sleeping {backoff}s before retry"
        )
        sleep_fn(backoff)


def run_tidal_dl(
    input_file: Path,
    *,
    output_dir: Path,
    quality: str = "high",
    tidal_dl_bin: str | None = None,
    extra_args: list[str] | None = None,
    chunk_size: int = 15,
    max_429_retries: int = 4,
    chunk_timeout: float = 300.0,
    inter_chunk_delay: float = 60.0,
    inter_chunk_jitter: float = 20.0,
    batch_pause_chunks: int = 30,
    batch_pause_duration: float = 900.0,
    sleep_fn=time.sleep,
    random_fn=random.random,
) -> int:
    """Stream every URL in `input_file` to `tiddl download url`.

    `input_file` is the same flat `.txt` produced by
    `build_tidal_input_file` (one `https://tidal.com/...` per line, with
    `#` comments tolerated). tiddl has no batch-input flag, so we chunk
    the URLs into batches of `chunk_size` and invoke the CLI per batch.

    Anti-ban measures (balanced for speed + safety):
    - Default quality "high" (not "max") to reduce API load.
    - Moderate chunks (default 15) with 60s delay + jitter between each.
    - After every 30 chunks (~450 tracks), pause for 15 min to cool down.
    - 429 rate-limit retry with exponential backoff.
    - chunk_timeout kills hung tiddl processes.

    Already-downloaded files are skipped by tiddl's default `--skip`
    behavior, so re-running is essentially free for the successes.
    Returns 0 if all chunks succeeded, otherwise the most recent
    non-zero exit code.
    """
    # tiddl's Rich console calls `.as_uri()` on the output path; a relative
    # path raises "relative paths can't be expressed as file URIs".
    # Resolve to absolute before passing the flag.
    output_dir = output_dir.resolve()
    tidal_dl_bin = tidal_dl_bin or _which_tiddl()
    urls: list[str] = []
    for line in input_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            urls.append(s)
    if not urls:
        print("[!] No Tidal URLs found in input file; nothing to do.")
        return 0
    print(
        f"[i] tiddl: {len(urls)} URLs, quality={quality!r}, "
        f"output={output_dir}, chunk={chunk_size}, "
        f"max_429_retries={max_429_retries}, "
        f"inter_chunk_delay={inter_chunk_delay}s, "
        f"batch_pause={batch_pause_chunks}chunks/{batch_pause_duration}s"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    last_rc = 0
    total_chunks = (len(urls) + chunk_size - 1) // chunk_size
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        cmd = [
            tidal_dl_bin,
            "download",
            "-p", str(output_dir),
            "-q", quality,
            "url",
            *chunk,
        ]
        if extra_args:
            cmd.extend(extra_args)
        rc = _run_chunk_with_429_retry(
            cmd,
            chunk_index=i // chunk_size + 1,
            total_chunks=total_chunks,
            chunk_size=len(chunk),
            max_429_retries=max_429_retries,
            sleep_fn=sleep_fn,
            chunk_timeout=chunk_timeout,
        )
        last_rc = rc if rc != 0 else last_rc
        # Anti-ban: delay between chunks with jitter.
        next_i = i + chunk_size
        if next_i < len(urls):
            jitter = inter_chunk_jitter * (2 * random_fn() - 1)  # ±jitter
            delay = max(0.0, inter_chunk_delay + jitter)
            # Anti-ban: long pause after every batch_pause_chunks chunks.
            chunk_num = (i // chunk_size) + 1
            if chunk_num % batch_pause_chunks == 0:
                print(
                    f"[i] Batch pause: completed {chunk_num} chunks, "
                    f"sleeping {batch_pause_duration}s to avoid rate limits"
                )
                sleep_fn(batch_pause_duration)
            elif delay > 0:
                print(
                    f"[i] Inter-chunk delay: sleeping {delay:.1f}s"
                )
                sleep_fn(delay)
    return last_rc


class TiddlDownloader(DownloaderBackend):
    @property
    def name(self) -> str:
        return "tiddl"

    def download(self, manifest: Manifest, cfg: AppConfig, **kwargs) -> int:
        """Build input file then invoke tiddl in anti-ban chunks.

        Input file is written to ``cfg.output_dir / kwargs.get('input_filename', 'tiddl-input.txt')``.
        Downloads land in ``cfg.tidal_download_dir`` at ``cfg.tidal_quality``.
        Extra kwargs (chunk_size, inter_chunk_delay, etc.) are forwarded to
        ``run_tidal_dl``.

        Returns the last non-zero tiddl exit code, or 0 on full success.
        """
        # Pop input_filename so it doesn't leak into run_tidal_dl (which rejects it)
        input_filename = kwargs.pop("input_filename", "tiddl-input.txt")
        input_path = cfg.output_dir / input_filename
        track_n, album_n = build_tidal_input_file(manifest, input_path)
        print(f"[tiddl] Wrote {track_n} tracks + {album_n} albums to {input_path}")
        return run_tidal_dl(
            input_path,
            output_dir=cfg.tidal_download_dir,
            quality=cfg.tidal_quality,
            **kwargs,
        )
