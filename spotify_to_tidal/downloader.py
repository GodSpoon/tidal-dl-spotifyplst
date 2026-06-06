"""Drive `tiddl` (https://github.com/oskvr37/tiddl) to download everything in a manifest.

We extract every `https://tidal.com/...` URL from the manifest and feed
them to `tiddl download url` in chunks. tiddl is preferred over the
older `tidal-dl` because it ships with a Tidal API key that the
current Tidal CDN accepts for playback URLs (the upstream tidal-dl
bundled key is now rate-limited / region-restricted for some accounts).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


from .manifest import Manifest


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

    # Playlists
    for pl in manifest.playlists:
        lines.append(f"# === Playlist: {pl.name} ({pl.track_count} tracks) ===")
        for t in pl.tracks:
            if t.matched and t.tidal_id:
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


def run_tidal_dl(
    input_file: Path,
    *,
    output_dir: Path,
    quality: str = "max",
    tidal_dl_bin: str | None = None,
    extra_args: list[str] | None = None,
    chunk_size: int = 200,
) -> int:
    """Stream every URL in `input_file` to `tiddl download url`.

    `input_file` is the same flat `.txt` produced by
    `build_tidal_input_file` (one `https://tidal.com/...` per line, with
    `#` comments tolerated). tiddl has no batch-input flag, so we chunk
    the URLs into batches of `chunk_size` and invoke the CLI per batch.
    Returns the exit code of the LAST chunk (0 = all good).
    """
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
        f"output={output_dir}, chunk={chunk_size}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    last_rc = 0
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
        print(f"[i] Chunk {i // chunk_size + 1}/{(len(urls) + chunk_size - 1) // chunk_size} "
              f"({len(chunk)} URLs)")
        rc = subprocess.call(cmd)
        last_rc = rc if rc != 0 else last_rc
        if rc != 0:
            print(f"[!] tiddl chunk exited with {rc}; continuing with next chunk")
    return last_rc



def print_summary(manifest: Manifest) -> None:
    s = manifest.stats
    if not s:
        manifest.compute_stats()
        s = manifest.stats
    print("\n========== Manifest Summary ==========")
    print(f"  Spotify user:        {manifest.spotify_user_id}")
    print(f"  Generated at:        {manifest.generated_at}")
    print(f"  Playlists:           {s.get('playlists', 0)}")
    print(
        f"  Playlist tracks:     {s.get('playlist_tracks', 0)} "
        f"({s.get('playlist_tracks_matched', 0)} matched, "
        f"{s.get('playlist_tracks_match_pct', 0):.1f}%)"
    )
    print(
        f"  Top artists:         {s.get('artists', 0)} "
        f"({s.get('artists_matched', 0)} matched)"
    )
    print(
        f"  Artist albums:       {s.get('artist_albums', 0)} "
        f"({s.get('artist_albums_matched', 0)} matched, "
        f"{s.get('artist_albums_match_pct', 0):.1f}%)"
    )
    print("======================================\n")
