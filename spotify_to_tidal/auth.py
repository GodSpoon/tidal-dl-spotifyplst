"""Spotify OAuth PKCE flow with a local callback HTTP server.

PKCE is the right choice for desktop/local tools: no client_secret
exposure, no need to pre-register a real public URL, and Spotify
treats `http://127.0.0.1:PORT/callback` as a loopback redirect.

Flow:
  1. Generate code_verifier (random) and code_challenge = base64url(SHA256(verifier)).
  2. Open the user's browser to the Spotify /authorize endpoint.
  3. Spin up a tiny http.server on 127.0.0.1:<port> that captures the
     redirected ?code=... response, then shuts itself down.
  4. Exchange code + verifier for {access_token, refresh_token, expires_in}.

Tokens are cached at ~/.config/spotify_to_tidal/spotify_token.json so
subsequent runs are silent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import http.server
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from .config import AppConfig, CONFIG_DIR

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


@dataclass
class SpotifyToken:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float  # epoch seconds
    scope: str

    def is_expired(self, skew: int = 60) -> bool:
        return time.time() >= self.expires_at - skew

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_json(cls, data: dict) -> "SpotifyToken":
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
            scope=data.get("scope", ""),
        )


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _new_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _parse_redirect_uri(uri: str) -> tuple[str, int, str]:
    """Return (host, port, path) from http://127.0.0.1:8888/callback."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"redirect_uri must be http(s); got {parsed.scheme}")
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"redirect_uri host must be a loopback address; got {parsed.hostname}"
        )
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path or "/"


def _token_cache_path() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR / "spotify_token.json"


def load_cached_token() -> Optional[SpotifyToken]:
    p = _token_cache_path()
    if not p.exists():
        return None
    try:
        return SpotifyToken.from_json(json.loads(p.read_text()))
    except Exception:
        return None


def _save_token(tok: SpotifyToken) -> None:
    p = _token_cache_path()
    p.write_text(json.dumps(tok.to_json(), indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _exchange_code_for_token(
    cfg: AppConfig, code: str, verifier: str
) -> SpotifyToken:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.spotify_redirect_uri,
        "client_id": cfg.spotify_client_id,
        "client_secret": cfg.spotify_client_secret,
        "code_verifier": verifier,
    }
    r = requests.post(TOKEN_URL, data=data, timeout=15)
    r.raise_for_status()
    body = r.json()
    return SpotifyToken(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + int(body.get("expires_in", 3600)),
        scope=body.get("scope", ""),
    )


def _refresh_token(cfg: AppConfig, refresh_token: str) -> SpotifyToken:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": cfg.spotify_client_id,
        "client_secret": cfg.spotify_client_secret,
    }
    r = requests.post(TOKEN_URL, data=data, timeout=15)
    r.raise_for_status()
    body = r.json()
    return SpotifyToken(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", refresh_token),
        expires_at=time.time() + int(body.get("expires_in", 3600)),
        scope=body.get("scope", ""),
    )


def get_token(cfg: AppConfig, *, force: bool = False) -> SpotifyToken:
    """Return a valid Spotify access token, refreshing or re-authing as needed."""
    if not force:
        cached = load_cached_token()
        if cached and not cached.is_expired():
            return cached
        if cached and cached.refresh_token:
            try:
                tok = _refresh_token(cfg, cached.refresh_token)
                _save_token(tok)
                return tok
            except requests.HTTPError as e:
                print(f"[!] Refresh failed ({e}); falling back to full auth.")

    return _interactive_pkce_login(cfg)


def _interactive_pkce_login(cfg: AppConfig) -> SpotifyToken:
    """Run the full PKCE login with a local callback server."""
    host, port, path = _parse_redirect_uri(cfg.spotify_redirect_uri)
    # Spin up a one-shot HTTP server on host:port that captures the code
    captured: dict[str, Optional[str]] = {"code": None, "error": None}
    event = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in qs:
                captured["code"] = qs["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Spotify login complete.</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                )
            elif "error" in qs:
                captured["error"] = qs["error"][0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<h2>Login failed: {qs['error'][0]}</h2>".encode())
            else:
                self.send_response(404)
                self.end_headers()
            event.set()

        def log_message(self, *a, **k):  # silence default access logging
            pass

    # Pick a port; if the configured redirect port is busy (likely means
    # the user is already running something on 8888) just use a free one
    # and hope Spotify's loopback allows it. We try the configured one first.
    try:
        port_to_use = port
        server = http.server.HTTPServer((host, port_to_use), Handler)
    except OSError:
        port_to_use = _free_port(host)
        server = http.server.HTTPServer((host, port_to_use), Handler)

    actual_uri = f"http://{host}:{port_to_use}{path}"

    verifier, challenge = _new_pkce_pair()
    params = {
        "client_id": cfg.spotify_client_id,
        "response_type": "code",
        "redirect_uri": actual_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": " ".join(cfg.scopes),
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print(f"[i] Opening browser to: {auth_url}")
    if not webbrowser.open(auth_url):
        print(
            f"[!] Couldn't open a browser. Open this URL manually:\n    {auth_url}\n"
        )

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if not event.wait(timeout=180):
            raise TimeoutError("Timed out waiting for Spotify callback.")
    finally:
        server.shutdown()

    if captured["error"]:
        raise RuntimeError(f"Spotify returned error: {captured['error']}")
    if not captured["code"]:
        raise RuntimeError("No authorization code received from Spotify.")

    token = _exchange_code_for_token(cfg, captured["code"], verifier)
    _save_token(token)
    return token
