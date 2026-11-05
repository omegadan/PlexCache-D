"""Recently Added with Jellyfin: the manager fetch and the web service wiring."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from core.jellyfin_api import JellyfinManager
from tests.test_jellyfin_pins import FakeResp as _PinResp, src


class FakeResp(_PinResp):
    @property
    def content(self):
        return b"" if self._payload is None else json.dumps(self._payload).encode()

NOW = datetime.now(timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")


MOVIES = [
    {"Id": "m-new", "Name": "New Film", "Type": "Movie", "DateCreated": iso(1), "MediaSources": [
        src("/data/movies/New - 1080p.mkv", 1920, 1080, 4_000),
        src("/data/movies/New - 2160p.mkv", 3840, 2160, 20_000)]},
    {"Id": "m-old", "Name": "Old Film", "Type": "Movie", "DateCreated": iso(30),
     "MediaSources": [src("/data/movies/Old.mkv", 1920, 1080, 1)]},
]
EPISODES = [
    {"Id": "e1", "Name": "Pilot", "Type": "Episode", "DateCreated": iso(2), "SeriesName": "Show",
     "SeriesId": "show-1", "ParentIndexNumber": 1, "IndexNumber": 1,
     "MediaSources": [src("/data/tv/Show/S01E01.mkv", 1280, 720, 500)]},
    {"Id": "e-nofile", "Name": "Virtual", "Type": "Episode", "DateCreated": iso(1), "SeriesName": "Show",
     "SeriesId": "show-1", "MediaSources": []},
]
VIEWS = [{"Id": "lib-m", "Name": "Movies", "CollectionType": "movies"},
         {"Id": "lib-t", "Name": "TV Shows", "CollectionType": "tvshows"},
         {"Id": "lib-k", "Name": "Kids", "CollectionType": "movies"},
         {"Id": "lib-x", "Name": "Music", "CollectionType": "music"}]


class Session:
    def __init__(self, broken=()):
        self.headers, self.broken, self.calls = {}, set(broken), []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        path = url.split("8096", 1)[1]
        self.calls.append((path, params))
        if path == "/System/Info":
            return FakeResp(200, {"ServerName": "JF"})
        if path == "/Users":
            return FakeResp(200, [{"Name": "dan", "Id": "u1", "Policy": {"IsAdministrator": True}}])
        if path == "/UserViews":
            return FakeResp(200, {"Items": VIEWS})
        if path == "/Items":
            lib = params["ParentId"]
            if lib in self.broken:
                return FakeResp(500)
            return FakeResp(200, {"Items": {"lib-m": MOVIES, "lib-t": EPISODES}.get(lib, [])})
        return FakeResp(404)


def manager(libraries=None, **kw):
    m = JellyfinManager("http://jf:8096", "key", main_user="dan", libraries=libraries,
                        session=Session(**kw), retry_limit=1, delay=0)
    m.connect()
    return m


def test_recently_added_items_match_plex_shape():
    result = manager().get_recently_added_media([], days_to_monitor=7, version_preference="1080p")
    assert result.unreadable_libraries == []
    assert [i.rating_key for i in result.items] == ["m-new", "e1"]  # newest first, old + fileless dropped

    film, ep = result.items
    assert film.file_path == "/data/movies/New - 1080p.mkv" and film.size == 4_000
    assert [(v["label"], v["is_preferred"]) for v in film.version_files] == [("1080p", True), ("4k", False)]
    assert film.library_title == "Movies" and film.added_at.tzinfo is None
    assert ep.media_type == "episode" and ep.show_rating_key == "show-1"
    assert ep.episode_info == {"show": "Show", "season": 1, "episode": 1}


def test_query_is_newest_first_and_library_scoped():
    m = manager()
    m.get_recently_added_media([], 7, max_items=50)
    item_calls = [p for path, p in m.http.calls if path == "/Items"]
    assert {p["ParentId"] for p in item_calls} == {"lib-m", "lib-t", "lib-k"}  # music skipped
    assert all(p["SortBy"] == "DateCreated" and p["SortOrder"] == "Descending" and p["Limit"] == 50
               for p in item_calls)


def test_library_filter_and_unreadable_library():
    m = manager(libraries=["Movies", "TV Shows"], broken={"lib-t"})
    result = m.get_recently_added_media([], 7)
    assert result.unreadable_libraries == ["TV Shows"]
    assert [i.rating_key for i in result.items] == ["m-new"]


def test_max_items_cap():
    assert len(manager().get_recently_added_media([], 7, max_items=1).items) == 1


def test_requires_connect():
    m = JellyfinManager("http://jf:8096", "key", session=Session())
    with pytest.raises(ConnectionError):
        m.get_recently_added_media([], 7)


def test_service_uses_jellyfin_in_jellyfin_mode(tmp_path):
    from web.services.recently_added_service import RecentlyAddedService
    settings = {
        "media_server": "jellyfin", "JELLYFIN_URL": "http://jf:8096", "JELLYFIN_API_KEY": "key",
        "jellyfin_user": "dan", "number_episodes": 5, "days_to_monitor": 7, "users_toggle": False,
        "watched_move": True, "cache_dir": "/mnt/cache/", "max_concurrent_moves_array": 2,
        "max_concurrent_moves_cache": 2,
        "path_mappings": [{"name": "All", "plex_path": "/data/", "real_path": "/mnt/user/",
                           "cache_path": "/mnt/cache/", "enabled": True, "cacheable": True}],
    }
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(settings))
    svc = RecentlyAddedService()
    svc.settings_file = str(f)

    real = manager()
    pinned = MagicMock()
    pinned.resolve_all_to_plex_path_map.return_value = {}
    with patch.object(svc, "_connect_jellyfin", return_value=real) as connect, \
         patch("core.plex_api.PlexManager") as plex_ctor, \
         patch("web.services.get_pinned_service", return_value=pinned):
        res = svc.get_recently_added(days=7, max_items=100)

    assert res["available"] is True, res["error"]
    assert not plex_ctor.called
    assert connect.call_args.args[0].jellyfin.jellyfin_user == "dan"
    assert [r.rating_key for r in res["rows"]] == ["m-new", "e1"]
    # Pin state was resolved through the Jellyfin pin adapter
    server = pinned.resolve_all_to_plex_path_map.call_args.kwargs["plex"]
    assert getattr(server, "accepts_string_keys", False) is True


def test_service_reports_missing_jellyfin_config(tmp_path):
    from web.services.recently_added_service import RecentlyAddedService
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"media_server": "jellyfin"}))
    svc = RecentlyAddedService()
    svc.settings_file = str(f)
    res = svc.get_recently_added()
    assert res["available"] is False
    assert "Jellyfin" in res["error"] or "configured" in res["error"]


def test_connect_jellyfin_builds_manager_from_config():
    from types import SimpleNamespace
    from web.services.recently_added_service import RecentlyAddedService
    config = SimpleNamespace(jellyfin=SimpleNamespace(
        jellyfin_url="http://jf:8096", jellyfin_api_key="key", jellyfin_user="dan",
        jellyfin_libraries=["Movies"]))
    with patch.object(JellyfinManager, "connect") as connect:
        m = RecentlyAddedService()._connect_jellyfin(config)
    assert isinstance(m, JellyfinManager) and connect.called
    assert (m.url, m.main_user_name, m.libraries, m.timeout) == ("http://jf:8096", "dan", ["Movies"], 30)
