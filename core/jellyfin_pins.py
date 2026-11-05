"""Jellyfin adapter for pinned media.

The pin code (``core.pinned_media``, ``web.services.pinned_service`` and
``core.pinned_cli``) was written against plexapi: it calls
``server.fetchItem(key)`` / ``server.search(...)`` and reads plexapi-style
attributes (``ratingKey``, ``media[].parts[].file``, ``seasons()``,
``episodes()``, ``parentTitle``...). ``JellyfinPinServer`` answers those same
calls from the Jellyfin REST API, so version selection, size estimates,
budgets and grouping stay shared between both backends.

Jellyfin item ids are GUID strings, so callers must not ``int()`` them; use
``core.pinned_media.fetch_pin_item`` which checks ``accepts_string_keys``.

Each Jellyfin MediaSource becomes one plexapi-style Media (Jellyfin lists
alternate versions of a movie/episode as extra MediaSources).
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests

ITEM_FIELDS = "MediaSources,Path,ChildCount,ProductionYear"

_TYPE_MAP = {"Movie": "movie", "Series": "show", "Season": "season", "Episode": "episode"}
_SEARCH_TYPES = {"movie": "Movie", "show": "Series"}


def _resolution_label(width: int, height: int) -> str:
    """Plex-style videoResolution from frame size ("4k", "1080", "720", "480", "sd")."""
    width, height = width or 0, height or 0
    if width >= 3200 or height >= 1800:
        return "4k"
    if width >= 1800 or height >= 1000:
        return "1080"
    if width >= 1200 or height >= 700:
        return "720"
    if height >= 470:
        return "480"
    return "sd"


def _media_from_source(source: Dict[str, Any]) -> Optional[SimpleNamespace]:
    if (source.get("Protocol") or "File") != "File" or not source.get("Path"):
        return None
    video = next((s for s in source.get("MediaStreams") or [] if s.get("Type") == "Video"), {})
    return SimpleNamespace(
        videoResolution=_resolution_label(video.get("Width"), video.get("Height")),
        bitrate=int((source.get("Bitrate") or 0) / 1000),  # kbps, like Plex
        parts=[SimpleNamespace(file=source["Path"], size=source.get("Size") or 0)],
    )


class JellyfinPinItem:
    """A Jellyfin item exposing the plexapi attributes the pin code reads."""

    def __init__(self, server: "JellyfinPinServer", data: Dict[str, Any]):
        self._server = server
        self.raw = data
        self.ratingKey = data.get("Id", "")
        self.title = data.get("Name", "")
        self.type = _TYPE_MAP.get(data.get("Type"), (data.get("Type") or "").lower())
        self.year = data.get("ProductionYear")
        self.index = data.get("IndexNumber")
        self.librarySectionTitle = ""
        self.leafCount = data.get("ChildCount")
        self.media = [m for m in (_media_from_source(s) for s in data.get("MediaSources") or []) if m]

        series_id = data.get("SeriesId", "")
        series_name = data.get("SeriesName", "")
        if self.type == "season":
            self.parentRatingKey = series_id
            self.parentTitle = series_name
            self.parentYear = None
        elif self.type == "episode":
            self.parentRatingKey = data.get("SeasonId", "")
            self.parentIndex = data.get("ParentIndexNumber")
            self.grandparentRatingKey = series_id
            self.grandparentTitle = series_name
            self.grandparentYear = None

    def seasons(self) -> List["JellyfinPinItem"]:
        return self._server._show_children(f"/Shows/{self.ratingKey}/Seasons")

    def episodes(self) -> List["JellyfinPinItem"]:
        # /Shows/{id}/Episodes also covers virtual seasons (no season folder)
        if self.type == "show":
            return self._server._show_children(f"/Shows/{self.ratingKey}/Episodes")
        return self._server._show_children(
            f"/Shows/{getattr(self, 'parentRatingKey', '')}/Episodes", seasonId=self.ratingKey)


class JellyfinPinServer:
    """plexapi ``PlexServer`` stand-in for pin lookups against Jellyfin."""

    accepts_string_keys = True

    def __init__(self, url: str, api_key: str, user_id: str = "",
                 session: Optional[requests.Session] = None, timeout: int = 30):
        self.url = (url or "").rstrip("/")
        self.user_id = user_id
        self.timeout = timeout
        self.http = session or requests.Session()
        if session is None:
            self.http.headers.update({"X-Emby-Token": api_key, "Accept": "application/json"})

    def _query(self, path: str, **params) -> List[Dict[str, Any]]:
        params.setdefault("Fields", ITEM_FIELDS)
        if self.user_id:
            params.setdefault("userId", self.user_id)
        resp = self.http.get(f"{self.url}{path}", params=params, timeout=self.timeout)
        if resp.status_code in (400, 404):
            raise ValueError(f"Jellyfin rejected {path} ({resp.status_code})")
        resp.raise_for_status()
        return (resp.json() or {}).get("Items") or []

    def _items(self, **params) -> List[Dict[str, Any]]:
        return self._query("/Items", **params)

    def _show_children(self, path: str, **params) -> List[JellyfinPinItem]:
        return [JellyfinPinItem(self, i) for i in self._query(path, **params)]

    def ping(self) -> None:
        """Raise if the server is unreachable or rejects the API key."""
        resp = self.http.get(f"{self.url}/System/Info", timeout=self.timeout)
        resp.raise_for_status()

    def fetchItem(self, key: Any) -> JellyfinPinItem:
        """Fetch one item by id. Raises ValueError when it doesn't exist."""
        key = str(key)
        items = self._items(Ids=key)
        if not items:
            raise ValueError(f"Jellyfin item {key} not found")
        return JellyfinPinItem(self, items[0])

    def search(self, query: str, mediatype: str = "movie", limit: int = 25) -> List[JellyfinPinItem]:
        jf_type = _SEARCH_TYPES.get(mediatype)
        if not jf_type:
            return []
        items = self._items(searchTerm=query, IncludeItemTypes=jf_type,
                            Recursive="true", Limit=limit)
        return [JellyfinPinItem(self, i) for i in items]
