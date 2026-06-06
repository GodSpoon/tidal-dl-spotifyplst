# Security

This document records the security model and a static-audit snapshot
for `spotify_to_tidal`. If you find a vulnerability, please open a
GitHub issue (or email, see below) — **do not** post a public PoC
before a fix is available.

## Threat model

The tool only ever calls three external services:

| Service  | Auth method        | Privileges                      |
|----------|--------------------|---------------------------------|
| Spotify  | OAuth 2.0 PKCE     | Read library, playlists, artists|
| Tidal    | Device-code OAuth  | Read catalog                    |
| User's filesystem | n/a      | Read/write manifest + downloads |

It does **not**:

- Accept any untrusted input from the network.
- Load code from a remote source.
- Use `eval`, `exec`, or pickle on user data.
- Run subprocesses with `shell=True`.
- Have a publicly reachable HTTP server (the Spotify callback is bound
  to `127.0.0.1` only; the host is validated in
  `auth.py:_parse_redirect_uri`).

## What we already do

- **PKCE on the Spotify side** — no `client_secret` ever leaves your
  machine after the one-time code-for-token exchange. The `code_verifier`
  is a 64-byte `secrets.token_bytes` value (`auth.py:_new_pkce_pair`).
- **Loopback-only callback** — `auth.py:_parse_redirect_uri` rejects
  any host that isn't `127.0.0.1`, `localhost`, or `::1`.
- **HTTPS only** — the only HTTP calls (`spotify.py`, `tidal.py`) use
  the `https://` scheme; `tidal.py` previously used `verify=False` for
  an apiKey probe, which was removed in the v1.0.0 audit.
- **Tokens never logged** — the access/refresh tokens are written to
  `~/.config/spotify_to_tidal/spotify_token.json` with mode `0o600` and
  are not echoed to stdout.
- **No telemetry, no auto-update channel.**

## Static audit

A `bandit -r spotify_to_tidal/` run reports:

| Severity | Count | Notes                                                          |
|----------|------:|----------------------------------------------------------------|
| High     | 0     |                                                                |
| Medium   | 0     |                                                                |
| Low      | 3     | All false positives (see below)                                |

### False positives (Low, accepted)

- `B105` `auth.py:38` — `TOKEN_URL = "https://accounts.spotify.com/api/token"`.
  Bandit flags any `= "...token..."` string; this is a public OAuth
  endpoint, not a secret.
- `B404` `downloader.py:13` — `import subprocess`. We invoke `tiddl`
  with a literal argv list (no shell). See `B603` below.
- `B603` `downloader.py:114` — `subprocess.call(cmd)`. `cmd` is built
  from a static list (`["tiddl", "download", ...]`) plus URLs from
  the manifest we just produced. Manifest URLs only contain
  `https://tidal.com/browse/{track|album}/<int>`, so the worst case
  is a malformed Tidal ID, not shell injection.

### Previously fixed

- `B501` `tidal.py:118` (HIGH, **fixed**) — `verify=False` on the
  apiKey probe POSTed to `https://auth.tidal.com/v1/oauth2/token`.
  Removed; SSL cert validation is now on by default.
- `B110` `tidal.py:142` (LOW, **fixed**) — bare `except: pass` on
  `TOKEN.save()`. Now logs the failure with `repr`.
- `B112` `tidal.py:128` (LOW, **fixed**) — bare `except: continue` in
  the apiKey probe loop. Now logs the failure with `repr` before
  trying the next key.

## Reporting a vulnerability

Open a private advisory on GitHub (Security tab → "Report a
vulnerability"). Please do not file a public issue.
