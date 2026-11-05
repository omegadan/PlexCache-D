"""
Jellyfin server interactions for PlexCache.

``JellyfinManager`` is a drop-in alternative to ``PlexManager`` for the core
caching run. It exposes the subset of the PlexManager surface that
``core.app.PlexCacheApp`` uses (connect, user loading, OnDeck, active sessions,
data-completeness flags) so the rest of the pipeline - path mapping, caching,
eviction, move-back - is unchanged.

Mapping of Plex concepts to Jellyfin:

* OnDeck            -> "Continue Watching" (resume) + "Next Up", per user.
* Next N episodes   -> following episodes of the series, chosen by the same
                       ``select_next_episodes`` policy Plex uses.
* Library sections  -> user views (libraries), selected by name or id via the
                       ``jellyfin_libraries`` setting.
* User tokens       -> not needed: one administrator API key can read every
                       user's state.
* Watchlist         -> not supported (Jellyfin has no watchlist).

Only the Jellyfin REST API is used (via ``requests``); no extra dependency.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import requests

from core.plex_api import OnDeckItem, select_next_episodes

CLIENT_NAME = "PlexCache-D"
DEVICE_NAME = "PlexCache-D"
DEVICE_ID = "plexcache-d-jellyfin"
CLIENT_VERSION = "1.0"

# Fields requested on item queries. Path + MediaSources give us the files on
# disk; the rest feeds episode metadata and the runtime-based prefetch.
ITEM_FIELDS = "Path,MediaSources,RunTimeTicks,ParentId"

# Jellyfin RunTimeTicks are 100ns units.
TICKS_PER_MS = 10_000

# Library collection types that never hold video PlexCache should cache.
# Box sets and playlists only re-list items from real libraries.
NON_VIDEO_COLLECTION_TYPES = {"music", "books", "photos", "playlists", "boxsets", "livetv", "folders"}

# Video library types offered by the setup wizards, with display labels.
VIDEO_LIBRARY_LABELS = {
    "movies": "Movies",
    "tvshows": "TV Shows",
    "homevideos": "Home Videos",
    "musicvideos": "Music Videos",
    "mixed": "Mixed",
    "": "Mixed",
}


# ----------------------------------------------------------------------
# Setup helpers (used by the web and CLI setup wizards)
# ----------------------------------------------------------------------
def _setup_get(url: str, api_key: str, path: str) -> Any:
    resp = requests.get(
        f"{url.rstrip('/')}{path}",
        headers={"X-Emby-Token": api_key, "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def validate_jellyfin_url(url: str) -> Optional[str]:
    """Return an error message for an unusable Jellyfin URL, else None."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Invalid URL"
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "URL must start with http:// or https:// and include a host"
    return None


def test_jellyfin_connection(url: str, api_key: str) -> tuple:
    """(ok, error, server_info) for a Jellyfin URL + API key."""
    error = validate_jellyfin_url(url)
    if error:
        return False, error, {}
    if not api_key:
        return False, "API key is required", {}
    try:
        resp = requests.get(
            f"{url.rstrip('/')}/System/Info",
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code in (401, 403):
            return False, "Jellyfin rejected the API key", {}
        resp.raise_for_status()
        return True, "", resp.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"Could not connect to Jellyfin: {e}", {}


# Not a pytest test despite the name.
test_jellyfin_connection.__test__ = False


def list_jellyfin_libraries(url: str, api_key: str) -> List[Dict[str, Any]]:
    """Video libraries as [{id, title, type_label, locations}].

    Raises ``requests.RequestException`` on connection failure.
    """
    libraries = []
    for folder in _setup_get(url, api_key, "/Library/VirtualFolders") or []:
        ctype = (folder.get("CollectionType") or "").lower()
        if ctype not in VIDEO_LIBRARY_LABELS:
            continue
        libraries.append({
            "id": folder.get("ItemId") or folder.get("Name"),
            "title": folder.get("Name", "Unknown"),
            "type_label": VIDEO_LIBRARY_LABELS[ctype],
            "locations": list(folder.get("Locations") or []),
        })
    return libraries


def list_jellyfin_users(url: str, api_key: str) -> List[Dict[str, Any]]:
    """Enabled users as [{title, id, is_admin}], admins first.

    Raises ``requests.RequestException`` on connection failure.
    """
    users = []
    for u in _setup_get(url, api_key, "/Users") or []:
        policy = u.get("Policy") or {}
        if policy.get("IsDisabled") or not u.get("Name"):
            continue
        users.append({
            "title": u["Name"],
            "id": u.get("Id"),
            "is_admin": bool(policy.get("IsAdministrator")),
        })
    users.sort(key=lambda u: (not u["is_admin"], u["title"].lower()))
    return users


@dataclass
class JellyfinSession:
    """An active playback session, reduced to what the app needs."""
    file_path: str
    username: str = ""
    title: str = ""


def _parse_jellyfin_date(value: Optional[str]) -> Optional[datetime]:
    """Parse a Jellyfin ISO-8601 timestamp into an aware UTC datetime.

    Jellyfin emits up to 7 fractional digits (``2024-05-01T12:34:56.1234567Z``),
    which ``datetime.fromisoformat`` rejects on older Pythons, so the seconds
    part is parsed and the fraction ignored.
    """
    if not value:
        return None
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc)


def _item_file_paths(item: Dict[str, Any]) -> List[str]:
    """Every local file behind an item, across all versions (MediaSources).

    Mirrors the Plex backend, which emits one OnDeckItem per media part.
    Remote sources (``Protocol`` other than ``File``, e.g. .strm/HTTP) are
    skipped because there is nothing on disk to cache.
    """
    paths: List[str] = []
    for source in item.get("MediaSources") or []:
        if source.get("Protocol", "File") != "File":
            continue
        path = source.get("Path")
        if path and path not in paths:
            paths.append(path)
    if not paths and item.get("Path") and item.get("LocationType", "FileSystem") == "FileSystem":
        paths.append(item["Path"])
    return paths


def _episode_view(item: Dict[str, Any]) -> SimpleNamespace:
    """Adapt a Jellyfin episode dict to the attributes ``select_next_episodes`` reads."""
    ticks = item.get("RunTimeTicks") or 0
    return SimpleNamespace(
        parentIndex=item.get("ParentIndexNumber"),
        index=item.get("IndexNumber"),
        title=item.get("Name", ""),
        grandparentTitle=item.get("SeriesName", ""),
        duration=(ticks // TICKS_PER_MS) if ticks else None,
        raw=item,
    )


class JellyfinManager:
    """Manages Jellyfin server connections and OnDeck-equivalent queries."""

    def __init__(self, jellyfin_url: str, api_key: str, main_user: str = "",
                 libraries: Optional[Sequence[str]] = None,
                 retry_limit: int = 3, delay: int = 5, timeout: Optional[int] = 30,
                 session: Optional[requests.Session] = None):
        self.url = (jellyfin_url or "").rstrip("/")
        self.api_key = api_key
        self.main_user_name = (main_user or "").strip()
        self.libraries = [str(lib).strip() for lib in (libraries or []) if str(lib).strip()]
        self.retry_limit = retry_limit
        self.delay = delay
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({
            "Authorization": (
                f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
                f'DeviceId="{DEVICE_ID}", Version="{CLIENT_VERSION}", Token="{api_key}"'
            ),
            # Older servers (<10.7) only read the legacy header.
            "X-Emby-Token": api_key,
            "Accept": "application/json",
        })
        self.server_info: Dict[str, Any] = {}
        # Kept for interface parity with PlexManager: some callers check
        # ``manager.plex`` for truthiness. Jellyfin has no plexapi object.
        self.plex = None

        self._users: Dict[str, Dict[str, Any]] = {}  # name -> user json
        self._main_user: Optional[Dict[str, Any]] = None
        self._tracked_users: List[str] = []  # names to fetch when users_toggle is on
        self._newly_discovered_users: List[dict] = []
        self._ondeck_data_complete = True
        self._watchlist_data_complete = True

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self.http.get(f"{self.url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    def _get_first(self, candidates: Sequence[tuple]) -> Any:
        """GET the first endpoint that exists.

        Jellyfin 10.9 moved several per-user routes (``/Users/{id}/Items/Resume``
        -> ``/UserItems/Resume?userId=``). The new route is tried first and the
        legacy one used on 404, so both old and new servers work.
        """
        last_error: Optional[Exception] = None
        for path, params in candidates:
            try:
                return self._get(path, params)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    last_error = e
                    continue
                raise
        if last_error:
            raise last_error
        return None

    # ------------------------------------------------------------------
    # Connection / users
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Connect to the Jellyfin server and verify the API key."""
        logging.debug(f"Connecting to Jellyfin server: {self.url}")
        if not self.url or not self.api_key:
            raise ConnectionError("Cannot connect to Jellyfin server: URL or API key not configured")
        try:
            self.server_info = self._get("/System/Info") or {}
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            reason = "invalid API key" if status in (401, 403) else f"HTTP {status}"
            logging.error(f"Cannot connect to Jellyfin server at {self.url}: {reason}")
            raise ConnectionError(f"Cannot connect to Jellyfin server: {reason}") from None
        except requests.RequestException as e:
            logging.error(f"Cannot connect to Jellyfin server at {self.url}: {e}")
            raise ConnectionError(f"Cannot connect to Jellyfin server: {e}") from None
        logging.debug(
            f"Jellyfin server: {self.server_info.get('ServerName', '?')} "
            f"version {self.server_info.get('Version', '?')}"
        )
        self._load_users()

    def _load_users(self) -> None:
        users = self._get("/Users") or []
        self._users = {}
        for u in users:
            if (u.get("Policy") or {}).get("IsDisabled"):
                continue
            if u.get("Name"):
                self._users[u["Name"]] = u

        if self.main_user_name:
            self._main_user = self._users.get(self.main_user_name)
            if not self._main_user:
                # Case-insensitive fallback - names are typed by hand in settings.
                lowered = {n.lower(): u for n, u in self._users.items()}
                self._main_user = lowered.get(self.main_user_name.lower())
            if not self._main_user:
                logging.warning(
                    f"[JELLYFIN] Configured jellyfin_user '{self.main_user_name}' not found; "
                    f"falling back to the first administrator"
                )
        if not self._main_user:
            admins = [u for u in self._users.values() if (u.get("Policy") or {}).get("IsAdministrator")]
            pool = admins or list(self._users.values())
            self._main_user = pool[0] if pool else None

        if self._main_user:
            logging.debug(f"[JELLYFIN] Main user: {self._main_user['Name']}")
        else:
            logging.error("[JELLYFIN] No enabled users found on the server")
            self._ondeck_data_complete = False

    def load_user_tokens(self, skip_users: Optional[List[str]] = None,
                         settings_users: Optional[List[dict]] = None,
                         main_username: Optional[str] = None) -> None:
        """Work out which users to track and which are new.

        Named for parity with ``PlexManager.load_user_tokens``. Jellyfin needs no
        per-user tokens; this compares the server's users with the settings
        file. Users already in settings are tracked unless both of their skip
        flags are set. Users not in settings are reported through
        ``get_newly_discovered_users`` so the app can add them with tracking
        off, the same as the Plex backend does.
        """
        skip = set(skip_users or [])
        known = {u.get("title") for u in (settings_users or []) if u.get("title")}
        main_name = self._main_user["Name"] if self._main_user else None

        self._tracked_users = []
        self._newly_discovered_users = []
        for name, user in self._users.items():
            if name == main_name:
                continue
            if name not in known:
                self._newly_discovered_users.append({
                    "title": name,
                    "id": user.get("Id"),
                    "is_admin": bool((user.get("Policy") or {}).get("IsAdministrator")),
                    "skip_ondeck": True,
                    "skip_watchlist": True,
                })
                continue
            if name in skip:
                continue
            self._tracked_users.append(name)

    def get_newly_discovered_users(self) -> List[dict]:
        return list(self._newly_discovered_users)

    # ------------------------------------------------------------------
    # Status flags (PlexManager parity)
    # ------------------------------------------------------------------
    def is_ondeck_data_complete(self) -> bool:
        return self._ondeck_data_complete

    def is_watchlist_data_complete(self) -> bool:
        return self._watchlist_data_complete

    def mark_watchlist_incomplete(self) -> None:
        self._watchlist_data_complete = False

    def get_pin_server(self):
        """plexapi-shaped server for pinned media lookups (shares this session)."""
        from core.jellyfin_pins import JellyfinPinServer
        return JellyfinPinServer(self.url, self.api_key, session=self.http,
                                 timeout=self.timeout or 30)

    def is_plex_tv_reachable(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Active sessions
    # ------------------------------------------------------------------
    def get_active_sessions(self) -> List[JellyfinSession]:
        """Return sessions that are currently playing a local file."""
        sessions = self._get("/Sessions", {"activeWithinSeconds": 960}) or []
        result: List[JellyfinSession] = []
        for s in sessions:
            item = s.get("NowPlayingItem")
            if not item:
                continue
            paths = _item_file_paths(item)
            if not paths and item.get("Id"):
                try:
                    detail = self._get("/Items", {"ids": item["Id"], "fields": ITEM_FIELDS,
                                                  "userId": s.get("UserId")})
                    items = (detail or {}).get("Items") or []
                    if items:
                        paths = _item_file_paths(items[0])
                except requests.RequestException as e:
                    logging.warning(f"[JELLYFIN] Could not resolve file for active session '{item.get('Name')}': {e}")
            # Prefer the source actually being played when there are several versions.
            playing_source = (s.get("PlayState") or {}).get("MediaSourceId")
            if playing_source:
                for src in item.get("MediaSources") or []:
                    if src.get("Id") == playing_source and src.get("Path"):
                        paths = [src["Path"]]
                        break
            title = item.get("Name", "")
            if item.get("SeriesName"):
                title = f"{item['SeriesName']} - {title}"
            for p in paths:
                result.append(JellyfinSession(file_path=p, username=s.get("UserName", ""), title=title))
        return result

    # ------------------------------------------------------------------
    # OnDeck
    # ------------------------------------------------------------------
    def _user_libraries(self, user_id: str) -> List[Dict[str, Any]]:
        data = self._get_first([
            ("/UserViews", {"userId": user_id}),
            (f"/Users/{user_id}/Views", None),
        ]) or {}
        views = [v for v in data.get("Items") or []
                 if (v.get("CollectionType") or "").lower() not in NON_VIDEO_COLLECTION_TYPES]
        if not self.libraries:
            return views
        wanted = {lib.lower() for lib in self.libraries}
        return [v for v in views
                if (v.get("Name") or "").lower() in wanted or (v.get("Id") or "").lower() in wanted]

    def _resume_items(self, user_id: str, parent_id: str) -> List[Dict[str, Any]]:
        params = {
            "parentId": parent_id,
            "fields": ITEM_FIELDS,
            "mediaTypes": "Video",
            "enableUserData": "true",
            "limit": 200,
        }
        data = self._get_first([
            ("/UserItems/Resume", dict(params, userId=user_id)),
            (f"/Users/{user_id}/Items/Resume", params),
        ]) or {}
        return data.get("Items") or []

    def _next_up_items(self, user_id: str, parent_id: str, days_to_monitor: int) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_monitor)
        data = self._get("/Shows/NextUp", {
            "userId": user_id,
            "parentId": parent_id,
            "fields": ITEM_FIELDS,
            "limit": 200,
            "nextUpDateCutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Resumable episodes already come from /Resume.
            "enableResumable": "false",
            "enableRewatching": "false",
            "disableFirstEpisode": "true",
        }) or {}
        return data.get("Items") or []

    def _series_episodes(self, user_id: str, series_id: str) -> List[Dict[str, Any]]:
        data = self._get(f"/Shows/{series_id}/Episodes", {
            "userId": user_id,
            "fields": ITEM_FIELDS,
        }) or {}
        return data.get("Items") or []

    def get_on_deck_media(self, valid_sections: List[Any], days_to_monitor: int,
                          number_episodes: int, users_toggle: bool, skip_ondeck: List[str],
                          per_user_days: Optional[Dict[str, int]] = None,
                          prefetch_minimum_minutes: int = 0) -> List[OnDeckItem]:
        """OnDeck-equivalent media for the main user and (optionally) other users.

        ``valid_sections`` is accepted for signature parity and ignored -
        Jellyfin libraries are chosen by ``jellyfin_libraries`` in settings.
        """
        if not self._main_user:
            self._ondeck_data_complete = False
            return []

        users = [self._main_user["Name"]]
        if users_toggle:
            for name in self._tracked_users:
                if name in (skip_ondeck or []):
                    logging.info(f"[USER:{name}] Skipping for OnDeck — in skip list")
                    continue
                users.append(name)

        results: List[OnDeckItem] = []
        with ThreadPoolExecutor(max_workers=min(10, len(users))) as executor:
            futures = {
                executor.submit(
                    self._fetch_user_on_deck_media, name,
                    (per_user_days or {}).get(name, days_to_monitor),
                    number_episodes, prefetch_minimum_minutes,
                ): name
                for name in users
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results.extend(future.result())
                except Exception as e:  # noqa: BLE001 - one user must not sink the run
                    logging.error(f"[USER:{name}] Failed to fetch Jellyfin OnDeck: {type(e).__name__}: {e}")
                    self._ondeck_data_complete = False

        by_user: Dict[str, int] = {}
        for item in results:
            by_user[item.username] = by_user.get(item.username, 0) + 1
            logging.debug(f"[USER:{item.username}] OnDeck found: {item.file_path}")
        for name in sorted(by_user):
            logging.debug(f"[USER:{name}] Found {by_user[name]} OnDeck items")
        return results

    def _fetch_user_on_deck_media(self, username: str, days_to_monitor: int,
                                  number_episodes: int,
                                  prefetch_minimum_minutes: int = 0) -> List[OnDeckItem]:
        user = self._users[username]
        user_id = user["Id"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_monitor)
        items: List[OnDeckItem] = []
        seen_ids: set = set()
        # Shows already expanded for this user - two OnDeck entries for the same
        # series (resume + next up) must not prefetch twice.
        expanded_series: Dict[str, List[Dict[str, Any]]] = {}

        libraries = self._user_libraries(user_id)
        if self.libraries and not libraries:
            logging.warning(
                f"[USER:{username}] None of the configured jellyfin_libraries "
                f"({', '.join(self.libraries)}) are visible to this user"
            )

        for lib in libraries:
            lib_id = lib["Id"]
            try:
                resume = self._resume_items(user_id, lib_id)
                next_up = self._next_up_items(user_id, lib_id, days_to_monitor)
            except requests.RequestException as e:
                logging.warning(f"[USER:{username}] Failed to fetch OnDeck for library '{lib.get('Name')}': {e}")
                self._ondeck_data_complete = False
                continue

            candidates = []
            for it in resume:
                last = _parse_jellyfin_date((it.get("UserData") or {}).get("LastPlayedDate"))
                if last and last < cutoff:
                    continue
                candidates.append(it)
            candidates.extend(next_up)

            for it in candidates:
                if it.get("Id") in seen_ids:
                    continue
                seen_ids.add(it.get("Id"))
                item_type = it.get("Type")
                if item_type == "Episode":
                    self._process_episode(it, user_id, username, number_episodes,
                                          prefetch_minimum_minutes, items, seen_ids, expanded_series)
                elif item_type in ("Movie", "Video", "MusicVideo"):
                    for path in _item_file_paths(it):
                        items.append(OnDeckItem(file_path=path, username=username,
                                                is_current_ondeck=True, rating_key=it.get("Id")))
                else:
                    logging.debug(f"Skipping OnDeck item '{it.get('Name')}' — unsupported type {item_type}")
        return items

    def _process_episode(self, ep: Dict[str, Any], user_id: str, username: str,
                         number_episodes: int, prefetch_minimum_minutes: int,
                         out: List[OnDeckItem], seen_ids: set,
                         expanded_series: Dict[str, List[Dict[str, Any]]]) -> None:
        show = ep.get("SeriesName", "")
        season = ep.get("ParentIndexNumber")
        number = ep.get("IndexNumber")
        info = {"show": show, "season": season, "episode": number} if season is not None and number is not None else None
        for path in _item_file_paths(ep):
            out.append(OnDeckItem(file_path=path, username=username, episode_info=info,
                                  is_current_ondeck=True, rating_key=ep.get("Id")))

        if season is None or number is None:
            logging.warning(f"Skipping next episode fetch for '{show}' - missing index data "
                            f"(season={season}, episode={number})")
            return
        series_id = ep.get("SeriesId")
        if not series_id:
            return
        if series_id not in expanded_series:
            expanded_series[series_id] = self._series_episodes(user_id, series_id)
        views = [_episode_view(e) for e in expanded_series[series_id]]
        for nxt in select_next_episodes(views, season, number, number_episodes,
                                        prefetch_minimum_minutes=prefetch_minimum_minutes):
            raw = nxt.raw
            if raw.get("Id") in seen_ids:
                continue
            seen_ids.add(raw.get("Id"))
            next_info = {"show": show, "season": nxt.parentIndex, "episode": nxt.index}
            for path in _item_file_paths(raw):
                out.append(OnDeckItem(file_path=path, username=username, episode_info=next_info,
                                      is_current_ondeck=False, rating_key=raw.get("Id")))
