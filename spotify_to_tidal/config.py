"""Configuration loading and shared state.

Reads .env (relative to the project root) for credentials, and provides
helpers for locating per-user cache/token files.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Per-user state lives under XDG_CONFIG_HOME if set, else ~/.config
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "spotify_to_tidal"
CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "spotify_to_tidal"


@dataclass
class AppConfig:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    tidal_download_dir: Path = field(
        default_factory=lambda: PROJECT_ROOT / "tidal_downloads"
    )
    tidal_quality: str = "max"
    # Where the user normally runs `tidal-dl` from (for its config files)
    tidal_dl_home: Optional[Path] = None
    scopes: list[str] = field(
        default_factory=lambda: [
            "playlist-read-private",
            "playlist-read-collaborative",
            "user-top-read",
            "user-library-read",
        ]
    )

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tidal_download_dir.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_env() -> dict[str, str]:
    """Load .env, but real environment variables take precedence."""
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        values.update({k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})
    for key in (
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REDIRECT_URI",
        "OUTPUT_DIR",
        "TIDAL_DOWNLOAD_DIR",
        "TIDAL_QUALITY",
    ):
        real = os.environ.get(key)
        if real:
            values[key] = real
    return values


def load_config() -> AppConfig:
    env = _load_env()
    try:
        cfg = AppConfig(
            spotify_client_id=env["SPOTIFY_CLIENT_ID"],
            spotify_client_secret=env["SPOTIFY_CLIENT_SECRET"],
            spotify_redirect_uri=env.get(
                "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
            ),
            output_dir=Path(env.get("OUTPUT_DIR", str(PROJECT_ROOT / "output"))).expanduser(),
            tidal_download_dir=Path(
                env.get("TIDAL_DOWNLOAD_DIR", str(PROJECT_ROOT / "tidal_downloads"))
            ).expanduser(),
            tidal_quality=env.get("TIDAL_QUALITY", "max"),
        )
    except KeyError as e:
        sys.stderr.write(
            f"\n[!] Missing required env var {e.args[0]}. "
            f"Copy .env.example to .env and fill it in.\n"
        )
        raise SystemExit(2)
    cfg.ensure_dirs()
    return cfg


# Tidal-dl stores its files under:
#   Linux:   ~/.local/share/tidal-dl/
#   macOS:   ~/Library/Application Support/tidal-dl/  (older versions) or
#            ~/.local/share/tidal-dl/  (newer)
# We probe a few locations.
def tidal_dl_paths() -> dict[str, Path]:
    home = Path.home()
    candidates = [
        home / ".local" / "share" / "tidal-dl",
        home / "Library" / "Application Support" / "tidal-dl",
        home / ".config" / "tidal-dl",
    ]
    chosen = next((p for p in candidates if p.exists()), candidates[0])
    return {
        "base": chosen,
        "settings": chosen / "config.json",
        "token": chosen / "token.json" if (chosen / "token.json").exists() else chosen / "TidalToken.json",
    }
