"""Command-line interface for spotify_to_tidal.

Subcommands:

  build       Build the Spotify manifest (no Tidal calls).
  match       Match an existing manifest to Tidal IDs.
  download    Run tiddl on a (matched) manifest.
  run         Do everything end-to-end. Default if no subcommand.
  show        Print a summary of an existing manifest.
  login       Just run the Spotify OAuth login (useful for first run).
  tidal-login Run / verify the tiddl device-code login.
Examples:

  python -m spotify_to_tidal login
  python -m spotify_to_tidal tidal-login
  python -m spotify_to_tidal run --manifest output/manifest.json
  python -m spotify_to_tidal build --manifest output/manifest.json --top 500
  python -m spotify_to_tidal match --manifest output/manifest.json
  python -m spotify_to_tidal download --manifest output/manifest.json
  python -m spotify_to_tidal show --manifest output/manifest.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .auth import get_token
from .config import load_config
from .downloader import print_summary
from .manifest import Manifest
from .pipeline import (
    build_manifest,
    download_from_manifest,
    match_manifest,
    run_all,
)
from .tidal import TidalNotLoggedIn, ensure_tidal_logged_in


def _manifest_path(args, cfg) -> Path:
    p = getattr(args, "manifest", None) or (cfg.output_dir / "manifest.json")
    return Path(p).expanduser()


def cmd_login(args, cfg):
    token = get_token(cfg, force=True)
    print(f"[OK] Spotify access token expires at {token.expires_at}")


def cmd_tidal_login(args, cfg):
    try:
        ensure_tidal_logged_in()
    except TidalNotLoggedIn as e:
        print(f"[ERR] {e}")
        return 1
    print("[OK] Tidal session is valid.")
    return 0


def cmd_build(args, cfg):
    m = build_manifest(
        cfg,
        top_artists_total=args.top,
        include_artist_albums=not args.no_artist_albums,
        include_compilations=not args.no_compilations,
    )
    out = _manifest_path(args, cfg)
    m.save_json(out)
    out.with_suffix(".csv").write_text("")  # touch
    m.save_csv(out.with_suffix(".csv"))
    print(f"[OK] Wrote {out} and {out.with_suffix('.csv')}")
    print_summary(m)
    return 0


def cmd_match(args, cfg):
    out = _manifest_path(args, cfg)
    if not out.exists():
        print(f"[ERR] No manifest at {out}. Run `build` first.")
        return 2
    m = Manifest.load_json(out)
    m = match_manifest(
        cfg, m,
        include_artist_albums=not args.no_artist_albums,
        include_playlists=not args.no_playlists,
        autosave_path=out,
    )
    m.save_json(out)
    m.save_csv(out.with_suffix(".csv"))
    print_summary(m)
    return 0


def cmd_download(args, cfg):
    out = _manifest_path(args, cfg)
    if not out.exists():
        print(f"[ERR] No manifest at {out}. Run `build` (and `match`) first.")
        return 2
    m = Manifest.load_json(out)
    download_from_manifest(cfg, m, input_filename=args.input_filename)
    return 0


def cmd_show(args, cfg):
    out = _manifest_path(args, cfg)
    if not out.exists():
        print(f"[ERR] No manifest at {out}.")
        return 2
    m = Manifest.load_json(out)
    m.compute_stats()
    print_summary(m)
    if args.verbose:
        # Show unmatched items for debugging
        for pl in m.playlists:
            bad = [t for t in pl.tracks if not t.matched]
            if bad:
                print(f"\n  Unmatched in '{pl.name}': {len(bad)}/{len(pl.tracks)}")
                for t in bad[:10]:
                    err = f" ({t.error})" if t.error else ""
                    print(f"    - {t.name} — {', '.join(t.artists)}{err}")
        for ar in m.artists:
            bad = [al for al in ar.albums if not al.matched]
            if bad:
                print(f"\n  Unmatched in '{ar.name}': {len(bad)}/{len(ar.albums)}")
                for al in bad[:10]:
                    err = f" ({al.error})" if al.error else ""
                    print(f"    - {al.name}{err}")
    return 0


def cmd_run(args, cfg):
    out = _manifest_path(args, cfg)
    try:
        run_all(
            cfg, out,
            top_artists_total=args.top,
            include_artist_albums=not args.no_artist_albums,
            include_playlists=not args.no_playlists,
            skip_download=args.skip_download,
        )
    except TidalNotLoggedIn as e:
        print(f"[ERR] {e}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spotify_to_tidal",
        description="Download a Spotify library via tidal-dl.",
    )
    p.add_argument(
        "--manifest", "-m",
        help="Path to the manifest JSON. Default: <output>/manifest.json",
    )
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("login", help="Run the Spotify OAuth PKCE flow once.")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("tidal-login", help="Verify or trigger the tiddl device-code login.")
    sp.set_defaults(func=cmd_tidal_login)

    sp = sub.add_parser("build", help="Build the Spotify manifest only (no Tidal calls).")
    sp.add_argument("--top", type=int, default=500, help="Top artists to fetch (default 500).")
    sp.add_argument("--no-artist-albums", action="store_true",
                    help="Skip fetching albums for top artists.")
    sp.add_argument("--no-compilations", action="store_true",
                    help="Don't include compilations/appearances.")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("match", help="Match an existing manifest to Tidal IDs.")
    sp.add_argument("--no-artist-albums", action="store_true",
                    help="Don't try to match artist albums.")
    sp.add_argument("--no-playlists", action="store_true",
                    help="Don't try to match playlist tracks.")
    sp.set_defaults(func=cmd_match)

    sp = sub.add_parser("download", help="Run tiddl on a matched manifest.")
    sp.add_argument("--input-filename", default="tiddl-input.txt",
                    help="Filename inside output_dir for the tiddl input file.")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("show", help="Print manifest summary.")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="Also list unmatched items.")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("run", help="Build + match + download in one go (default).")
    sp.add_argument("--top", type=int, default=500, help="Top artists to fetch (default 500).")
    sp.add_argument("--no-artist-albums", action="store_true")
    sp.add_argument("--no-playlists", action="store_true")
    sp.add_argument("--skip-download", action="store_true",
                    help="Stop after building + matching; don't call tidal-dl.")
    sp.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args.cmd = "run"
        args.func = cmd_run
        args.top = 500
        args.no_artist_albums = False
        args.no_playlists = False
        args.skip_download = False
        args.input_filename = "tidal-dl-input.txt"
        args.verbose = False
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
