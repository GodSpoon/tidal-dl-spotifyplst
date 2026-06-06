"""Tidal search & lookup, reusing the user's existing tidal-dl login.

We import the `tidal_dl` package directly and call its `TIDAL_API` singleton.
That gives us:

  * `search(query, type, limit)` — for matching Spotify content to Tidal.
  * `getByString(s)`            — for resolving playlist/album/track IDs.
  * `getArtistAlbums(id, ...)`  — for fetching all albums for an artist.

For this to work the user must have already run `tidal-dl` once and
completed the device-code login flow. If not, we trigger it.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Importing tidal_dl has a side effect of reading its settings + token from
# disk. We do that lazily inside TidalClient so the rest of the tool can
# import this module without a logged-in Tidal session.


@dataclass
class TidalTrack:
    id: int
    title: str
    duration: int
    artist: str
    artists: list[str]
    album: str
    isrc: str
    version: str
    explicit: bool
    audio_quality: str


@dataclass
class TidalAlbum:
    id: int
    title: str
    artist: str
    artists: list[str]
    release_date: str
    num_tracks: int
    duration: int
    explicit: bool


@dataclass
class TidalArtist:
    id: int
    name: str


@dataclass
class TidalPlaylist:
    uuid: str
    title: str
    num_tracks: int


class TidalNotLoggedIn(RuntimeError):
    """Raised when we can't find a valid Tidal session."""


def _tidal_dl_module():
    """Import tidal_dl; on macOS pipx we need its venv on sys.path."""
    # The tidal-dl venv site-packages we discovered earlier lives at
    # /Users/sking/.local/pipx/venvs/tidal-dl/lib/python3.14/site-packages
    pipx_base = Path.home() / ".local" / "pipx" / "venvs" / "tidal-dl" / "lib"
    if pipx_base.exists():
        for p in pipx_base.glob("python*/site-packages"):
            sp = str(p)
            if sp not in sys.path:
                sys.path.insert(0, sp)
    import tidal_dl  # noqa: F401

    return tidal_dl


def _pick_working_api_key(tidal_dl_mod, relogin: bool = False):
    """Find an apiKey index that the Tidal API still accepts.

    The user's `SETTINGS.apiKeyIndex` may point at a key that Tidal has
    since banned — the device-code endpoint returns a URL, but the
    token-polling endpoint then returns 403 HTML, which makes the
    upstream `loginByWeb` loop raise a JSONDecodeError. We probe by
    fetching a device code *and* doing one token poll, accepting only
    keys that yield a parseable JSON body.

    When `relogin=True` and the chosen key differs from the saved one,
    the existing access/refresh tokens are wiped and `loginByWeb` is
    re-invoked so the new tokens are issued by the new client_id.
    Returns the chosen index, or None on failure.
    """
    import requests

    SETTINGS = tidal_dl_mod.settings.SETTINGS  # type: ignore
    api = tidal_dl_mod.tidal.TIDAL_API
    candidates = [SETTINGS.apiKeyIndex] + [
        i for i in range(tidal_dl_mod.apiKey.getNum()) if i != SETTINGS.apiKeyIndex
    ]
    for idx in candidates:
        api.apiKey = tidal_dl_mod.apiKey.getItem(idx)
        try:
            api.getDeviceCode()
            data = {
                'client_id': api.apiKey['clientId'],
                'device_code': api.key.deviceCode,
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'scope': 'r_usr+w_usr+w_sub',
            }
            auth = (api.apiKey['clientId'], api.apiKey['clientSecret'])
            r = requests.post(
                'https://auth.tidal.com/v1/oauth2/token',
                data=data, auth=auth, timeout=10,
            )
            r.json()  # raises ValueError on HTML/empty
            if relogin and idx != SETTINGS.apiKeyIndex:
                SETTINGS.apiKeyIndex = idx
                SETTINGS.save()
                _wipe_token(tidal_dl_mod)
                if not tidal_dl_mod.events.loginByWeb():
                    return None
            return idx
        except Exception as e:
            # Probing failed (network, JSON decode, etc.) — try next key.
            print(f"[i] apiKey[{idx}] probe failed: {e!r}")
            continue
    return None



def _wipe_token(tidal_dl_mod) -> None:
    from tidal_dl.settings import TOKEN  # type: ignore
    TOKEN.accessToken = ""
    TOKEN.refreshToken = ""
    TOKEN.userid = None
    TOKEN.countryCode = None
    TOKEN.expiresAfter = 0
    try:
        TOKEN.save()
    except Exception as e:
        print(f"[!] Could not write Tidal token to disk: {e!r}")




def _hydrate_tidal_api(tidal_dl_mod) -> bool:
    """Make sure the in-memory TIDAL_API singleton is ready to make calls.

    Reads the saved token from disk, refreshes it if it's about to expire,
    and copies the userId / countryCode / accessToken into
    `TIDAL_API.key` so the upstream `__get__` actually has a countryCode
    to send. Returns True when the API is ready, False if no token is
    saved (caller should run a device-code login).
    """
    from tidal_dl.settings import TOKEN, SETTINGS  # type: ignore
    from tidal_dl.paths import getProfilePath, getTokenPath  # type: ignore

    SETTINGS.read(getProfilePath())
    TOKEN.read(getTokenPath())
    api = tidal_dl_mod.tidal.TIDAL_API

    def _copy_into_api():
        api.key.userId = TOKEN.userid
        api.key.countryCode = TOKEN.countryCode
        api.key.accessToken = TOKEN.accessToken
        if TOKEN.expiresAfter:
            api.key.expiresIn = max(0, int(TOKEN.expiresAfter - time.time()))

    if not TOKEN.accessToken:
        return False
    if TOKEN.expiresAfter and time.time() < TOKEN.expiresAfter - 60:
        _copy_into_api()
        return True
    # Try refresh (works only if the access/refresh tokens were issued
    # by the same client_id that TIDAL_API.apiKey currently uses; if
    # the picker switched the key, the existing tokens are no good).
    try:
        ok = api.refreshAccessToken(TOKEN.refreshToken)
    except Exception:
        ok = False
    if ok:
        TOKEN.userid = api.key.userId
        TOKEN.countryCode = api.key.countryCode
        TOKEN.accessToken = api.key.accessToken
        TOKEN.expiresAfter = time.time() + int(api.key.expiresIn)
        TOKEN.save()
        return True
    return False


def _switch_api_key_and_relogin(tidal_dl_mod, new_index: int) -> bool:
    """Switch the apiKey in-memory and on disk, then re-issue tokens.

    Switching the apiKey invalidates the existing access/refresh tokens
    (they're bound to the old client_id), so we have to run the
    device-code flow again. Caller is responsible for prompting the
    user to visit the returned URL.
    """
    from tidal_dl.settings import SETTINGS  # type: ignore
    from tidal_dl.paths import getProfilePath  # type: ignore

    SETTINGS.read(getProfilePath())
    SETTINGS.apiKeyIndex = new_index
    SETTINGS.save()
    # Wipe the in-memory singleton so it re-reads SETTINGS next time.
    from tidal_dl.settings import TOKEN  # type: ignore
    TOKEN.accessToken = ""
    TOKEN.refreshToken = ""
    return bool(tidal_dl_mod.events.loginByWeb())



# Backwards-compatible alias used by ensure_tidal_logged_in.
_has_valid_token = _hydrate_tidal_api



def _login_device_code(tidal_dl_mod) -> bool:
    """Trigger tidal-dl's web (device-code) login flow."""
    from tidal_dl.settings import TOKEN, SETTINGS  # type: ignore
    from tidal_dl.paths import getProfilePath, getTokenPath  # type: ignore

    SETTINGS.read(getProfilePath())
    TOKEN.read(getTokenPath())
    chosen = _pick_working_api_key(tidal_dl_mod)
    if chosen != SETTINGS.apiKeyIndex:
        SETTINGS.apiKeyIndex = chosen
        SETTINGS.save()
    return bool(tidal_dl_mod.events.loginByWeb())



def ensure_tidal_logged_in() -> None:
    """Make sure the user is logged in to Tidal via the tidal-dl SDK
    (used for the search/match stage). Idempotent.

    The download stage uses `tiddl` separately; run `tiddl auth login`
    once to authorise that. This function only worries about the
    search-side session.
    """
    tidal_dl_mod = _tidal_dl_module()
    if _hydrate_tidal_api(tidal_dl_mod):
        return
    print("[i] No valid Tidal session found. Starting device-code login...")
    _wipe_token(tidal_dl_mod)
    if not _login_device_code(tidal_dl_mod):
        raise TidalNotLoggedIn(
            "Tidal login failed. Run `tidal-dl` once interactively to log in, "
            "then re-run this command. Also run `tiddl auth login` for downloads."
        )


class TidalClient:
    """Thin wrapper over tidal_dl.tidal.TIDAL_API with caching."""

    def __init__(self):
        self._tidal_dl = _tidal_dl_module()
        # Touch the settings/token to make sure the singleton is hydrated.
        from tidal_dl.settings import SETTINGS  # type: ignore
        from tidal_dl.paths import getProfilePath  # type: ignore

        SETTINGS.read(getProfilePath())
        _hydrate_tidal_api(self._tidal_dl)
        # Pick the healthiest apiKey; if it differs from the saved one,
        # the existing access/refresh tokens are now invalid, so wipe
        # them and re-hydrate (which will then take the no-token path
        # and force a re-login on the next ensure_tidal_logged_in call).
        chosen = _pick_working_api_key(self._tidal_dl)
        if chosen is not None and chosen != SETTINGS.apiKeyIndex:
            SETTINGS.apiKeyIndex = chosen
            SETTINGS.save()
            _wipe_token(self._tidal_dl)
            _hydrate_tidal_api(self._tidal_dl)
        self._api = self._tidal_dl.tidal.TIDAL_API
        self._search_cache: dict[tuple[str, str], list] = {}
        # Tidal search has hard limits and we want batched lookups
        self._albums_by_artist: dict[int, list[TidalAlbum]] = {}



    # ---------- raw search ----------

    def _search(self, query: str, type_name: str, limit: int = 10):
        """Search Tidal; returns a list of Tidal SDK objects."""
        from tidal_dl.enums import Type  # type: ignore

        type_map = {
            "tracks": Type.Track,
            "albums": Type.Album,
            "artists": Type.Artist,
            "playlists": Type.Playlist,
        }
        cache_key = (query.strip().lower(), type_name)
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        try:
            res = self._api.search(query, type_map[type_name], 0, limit)
        except Exception as e:
            # CountryCode missing or 401 etc. — return empty.
            print(f"[!] Tidal search failed for {query!r} ({type_name}): {e}")
            self._search_cache[cache_key] = []
            return []
        if type_name == "tracks":
            items = (res.tracks.items or []) if res.tracks else []
        elif type_name == "albums":
            items = (res.albums.items or []) if res.albums else []
        elif type_name == "artists":
            items = (res.artists.items or []) if res.artists else []
        elif type_name == "playlists":
            items = (res.playlists.items or []) if res.playlists else []
        else:
            items = []
        self._search_cache[cache_key] = items
        return items

    # ---------- typed results ----------

    @staticmethod
    def _track_name(t) -> str:
        return (t.title or "").strip()

    @staticmethod
    def _track_artist_name(t) -> str:
        a = t.artist
        return (a.name if a else "") or ""

    @staticmethod
    def _track_all_artist_names(t) -> list[str]:
        if t.artists and isinstance(t.artists, list):
            return [a.name for a in t.artists if a and a.name]
        return [TidalClient._track_artist_name(t)] if t.artist and t.artist.name else []

    @staticmethod
    def _track_album_name(t) -> str:
        a = t.album
        return (a.title if a else "") or ""

    @staticmethod
    def _album_artist_name(a) -> str:
        if a.artist and a.artist.name:
            return a.artist.name
        if a.artists and isinstance(a.artists, list) and a.artists:
            return a.artists[0].name or ""
        return ""

    @staticmethod
    def _album_all_artist_names(a) -> list[str]:
        if a.artists and isinstance(a.artists, list):
            return [x.name for x in a.artists if x and x.name]
        return [TidalClient._album_artist_name(a)] if TidalClient._album_artist_name(a) else []

    def search_tracks(self, query: str, limit: int = 10) -> list[TidalTrack]:
        out: list[TidalTrack] = []
        for t in self._search(query, "tracks", limit):
            out.append(
                TidalTrack(
                    id=int(t.id),
                    title=self._track_name(t),
                    duration=int(t.duration or 0),
                    artist=self._track_artist_name(t),
                    artists=self._track_all_artist_names(t),
                    album=self._track_album_name(t),
                    isrc=(t.isrc or "") if hasattr(t, "isrc") else "",
                    version=(t.version or "") if hasattr(t, "version") else "",
                    explicit=bool(getattr(t, "explicit", False)),
                    audio_quality=str(getattr(t, "audioQuality", "") or ""),
                )
            )
        return out

    def search_albums(self, query: str, limit: int = 10) -> list[TidalAlbum]:
        out: list[TidalAlbum] = []
        for a in self._search(query, "albums", limit):
            out.append(
                TidalAlbum(
                    id=int(a.id),
                    title=(a.title or "").strip(),
                    artist=self._album_artist_name(a),
                    artists=self._album_all_artist_names(a),
                    release_date=(a.releaseDate or "") or "",
                    num_tracks=int(a.numberOfTracks or 0),
                    duration=int(a.duration or 0),
                    explicit=bool(getattr(a, "explicit", False)),
                )
            )
        return out

    def search_artists(self, query: str, limit: int = 5) -> list[TidalArtist]:
        out: list[TidalArtist] = []
        for a in self._search(query, "artists", limit):
            out.append(TidalArtist(id=int(a.id), name=(a.name or "").strip()))
        return out

    def search_playlists(self, query: str, limit: int = 5) -> list[TidalPlaylist]:
        out: list[TidalPlaylist] = []
        for p in self._search(query, "playlists", limit):
            out.append(
                TidalPlaylist(
                    uuid=str(p.uuid),
                    title=(p.title or "").strip(),
                    num_tracks=int(p.numberOfTracks or 0),
                )
            )
        return out

    # ---------- artist deep-dive ----------

    def get_artist_albums(self, artist_id: int, include_ep: bool = True) -> list[TidalAlbum]:
        if artist_id in self._albums_by_artist:
            return self._albums_by_artist[artist_id]
        try:
            items = self._api.getArtistAlbums(artist_id, include_ep)
        except Exception as e:
            print(f"[!] getArtistAlbums({artist_id}) failed: {e}")
            self._albums_by_artist[artist_id] = []
            return []
        out = [
            TidalAlbum(
                id=int(a.id),
                title=(a.title or "").strip(),
                artist=self._album_artist_name(a),
                artists=self._album_all_artist_names(a),
                release_date=(a.releaseDate or "") or "",
                num_tracks=int(a.numberOfTracks or 0),
                duration=int(a.duration or 0),
                explicit=bool(getattr(a, "explicit", False)),
            )
            for a in (items or [])
        ]
        self._albums_by_artist[artist_id] = out
        return out
