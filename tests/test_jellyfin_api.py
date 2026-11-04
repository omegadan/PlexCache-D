"""Tests for the Jellyfin backend (core/jellyfin_api.py)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from core.jellyfin_api import JellyfinManager, JellyfinSession, _parse_jellyfin_date


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"" if payload is None else json.dumps(payload).encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Routes GETs to handlers keyed by URL path; unknown paths 404."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        self.calls.append((path, dict(params or {})))
        handler = self.routes.get(path)
        if handler is None:
            return FakeResponse(404, None)
        result = handler(params or {}) if callable(handler) else handler
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(200, result)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")


NOW = datetime.now(timezone.utc)

USERS = [
    {"Name": "admin", "Id": "u-admin", "Policy": {"IsAdministrator": True}},
    {"Name": "kid", "Id": "u-kid", "Policy": {"IsAdministrator": False}},
    {"Name": "ghost", "Id": "u-ghost", "Policy": {"IsDisabled": True}},
]

VIEWS = {"Items": [
    {"Id": "lib-movies", "Name": "Movies", "CollectionType": "movies"},
    {"Id": "lib-tv", "Name": "TV Shows", "CollectionType": "tvshows"},
    {"Id": "lib-music", "Name": "Music", "CollectionType": "music"},
]}


def episode(eid, season, number, ticks=27_000_000_000, path=None):
    return {
        "Id": eid, "Type": "Episode", "Name": f"Ep {number}",
        "SeriesName": "Show", "SeriesId": "series-1",
        "ParentIndexNumber": season, "IndexNumber": number,
        "RunTimeTicks": ticks,
        "MediaSources": [{"Protocol": "File", "Path": path or f"/media/tv/Show/S{season:02}E{number:02}.mkv"}],
    }


SERIES_EPISODES = {"Items": [episode(f"e{n}", 1, n) for n in range(1, 8)]}


def make_routes(resume_by_lib=None, nextup_by_lib=None, users=USERS):
    resume_by_lib = resume_by_lib or {}
    nextup_by_lib = nextup_by_lib or {}
    return {
        "/System/Info": {"ServerName": "jf", "Version": "10.10.0"},
        "/Users": users,
        "/UserViews": VIEWS,
        "/UserItems/Resume": lambda p: {"Items": resume_by_lib.get(p.get("parentId"), [])},
        "/Shows/NextUp": lambda p: {"Items": nextup_by_lib.get(p.get("parentId"), [])},
        "/Shows/series-1/Episodes": SERIES_EPISODES,
    }


def make_manager(routes, **kwargs):
    mgr = JellyfinManager("http://jf:8096/", "key", session=FakeSession(routes), **kwargs)
    mgr.connect()
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_parse_date_handles_seven_digit_fraction():
    dt = _parse_jellyfin_date("2024-05-01T12:34:56.1234567Z")
    assert dt == datetime(2024, 5, 1, 12, 34, 56, tzinfo=timezone.utc)
    assert _parse_jellyfin_date(None) is None
    assert _parse_jellyfin_date("garbage") is None


def test_auth_header_and_url_normalised():
    mgr = make_manager(make_routes())
    assert mgr.url == "http://jf:8096"
    assert 'Token="key"' in mgr.http.headers["Authorization"]
    assert mgr.http.headers["X-Emby-Token"] == "key"


def test_connect_bad_key_raises_connection_error():
    routes = {"/System/Info": FakeResponse(401, None)}
    mgr = JellyfinManager("http://jf", "bad", session=FakeSession(routes))
    with pytest.raises(ConnectionError, match="invalid API key"):
        mgr.connect()


def test_main_user_defaults_to_first_admin_and_disabled_users_ignored():
    mgr = make_manager(make_routes())
    assert mgr._main_user["Name"] == "admin"
    assert "ghost" not in mgr._users


def test_configured_main_user_is_case_insensitive():
    mgr = make_manager(make_routes(), main_user="KID")
    assert mgr._main_user["Name"] == "kid"


def test_movie_resume_and_days_to_monitor_cutoff():
    resume = {"lib-movies": [
        {"Id": "m1", "Type": "Movie", "Name": "Recent",
         "UserData": {"LastPlayedDate": iso(NOW - timedelta(days=1))},
         "MediaSources": [{"Protocol": "File", "Path": "/media/movies/Recent.mkv"},
                          {"Protocol": "File", "Path": "/media/movies/Recent-4k.mkv"}]},
        {"Id": "m2", "Type": "Movie", "Name": "Old",
         "UserData": {"LastPlayedDate": iso(NOW - timedelta(days=400))},
         "MediaSources": [{"Protocol": "File", "Path": "/media/movies/Old.mkv"}]},
        {"Id": "m3", "Type": "Movie", "Name": "Stream",
         "UserData": {"LastPlayedDate": iso(NOW)},
         "MediaSources": [{"Protocol": "Http", "Path": "http://x/y.strm"}]},
    ]}
    mgr = make_manager(make_routes(resume_by_lib=resume))
    items = mgr.get_on_deck_media([], 30, 3, False, [])
    assert [i.file_path for i in items] == ["/media/movies/Recent.mkv", "/media/movies/Recent-4k.mkv"]
    assert all(i.is_current_ondeck and i.rating_key == "m1" and i.username == "admin" for i in items)
    assert mgr.is_ondeck_data_complete()


def test_music_library_is_never_queried():
    mgr = make_manager(make_routes())
    mgr.get_on_deck_media([], 30, 3, False, [])
    queried = {p.get("parentId") for path, p in mgr.http.calls if path == "/UserItems/Resume"}
    assert queried == {"lib-movies", "lib-tv"}


def test_library_filter_by_name_or_id():
    mgr = make_manager(make_routes(), libraries=["tv shows"])
    mgr.get_on_deck_media([], 30, 3, False, [])
    queried = {p.get("parentId") for path, p in mgr.http.calls if path == "/UserItems/Resume"}
    assert queried == {"lib-tv"}


def test_next_up_episode_prefetches_following_episodes():
    nextup = {"lib-tv": [episode("e3", 1, 3)]}
    mgr = make_manager(make_routes(nextup_by_lib=nextup))
    items = mgr.get_on_deck_media([], 30, 2, False, [])
    assert [(i.episode_info["episode"], i.is_current_ondeck) for i in items] == [
        (3, True), (4, False), (5, False)]
    # NextUp is asked for unresumed, recently-watched shows only
    params = next(p for path, p in mgr.http.calls if path == "/Shows/NextUp")
    assert params["enableResumable"] == "false"
    assert params["nextUpDateCutoff"].endswith("Z")


def test_prefetch_minimum_minutes_extends_episode_count():
    # 45-minute episodes; 2 episodes minimum but 120 minutes => 3 episodes
    nextup = {"lib-tv": [episode("e1", 1, 1)]}
    mgr = make_manager(make_routes(nextup_by_lib=nextup))
    items = mgr.get_on_deck_media([], 30, 2, False, [], prefetch_minimum_minutes=120)
    assert [i.episode_info["episode"] for i in items] == [1, 2, 3, 4]


def test_resume_and_next_up_for_same_show_do_not_duplicate():
    resume = {"lib-tv": [dict(episode("e2", 1, 2), UserData={"LastPlayedDate": iso(NOW)})]}
    nextup = {"lib-tv": [episode("e3", 1, 3)]}
    mgr = make_manager(make_routes(resume_by_lib=resume, nextup_by_lib=nextup))
    items = mgr.get_on_deck_media([], 30, 2, False, [])
    paths = [i.file_path for i in items]
    assert len(paths) == len(set(paths))
    # Series episode list fetched once per user
    assert sum(1 for path, _ in mgr.http.calls if path == "/Shows/series-1/Episodes") == 1


def test_users_toggle_tracks_known_users_and_reports_new_ones():
    resume = {"lib-movies": [{"Id": "m1", "Type": "Movie", "UserData": {"LastPlayedDate": iso(NOW)},
                              "MediaSources": [{"Protocol": "File", "Path": "/m.mkv"}]}]}
    mgr = make_manager(make_routes(resume_by_lib=resume))

    # kid not in settings yet -> discovered, not tracked
    mgr.load_user_tokens(skip_users=[], settings_users=[])
    assert [u["title"] for u in mgr.get_newly_discovered_users()] == ["kid"]
    assert mgr.get_newly_discovered_users()[0]["skip_ondeck"] is True
    assert {i.username for i in mgr.get_on_deck_media([], 30, 1, True, [])} == {"admin"}

    # kid in settings and not skipped -> tracked
    mgr.load_user_tokens(skip_users=[], settings_users=[{"title": "kid"}])
    assert mgr.get_newly_discovered_users() == []
    assert {i.username for i in mgr.get_on_deck_media([], 30, 1, True, [])} == {"admin", "kid"}

    # skipped for OnDeck
    assert {i.username for i in mgr.get_on_deck_media([], 30, 1, True, ["kid"])} == {"admin"}


def test_legacy_routes_used_when_new_ones_missing():
    routes = make_routes(resume_by_lib={})
    views = routes.pop("/UserViews")
    routes.pop("/UserItems/Resume")
    routes["/Users/u-admin/Views"] = views
    routes["/Users/u-admin/Items/Resume"] = {"Items": [
        {"Id": "m1", "Type": "Movie", "UserData": {"LastPlayedDate": iso(NOW)},
         "MediaSources": [{"Protocol": "File", "Path": "/old.mkv"}]}]}
    mgr = make_manager(routes)
    items = mgr.get_on_deck_media([], 30, 1, False, [])
    assert {i.file_path for i in items} == {"/old.mkv"}
    assert mgr.is_ondeck_data_complete()


def test_library_fetch_failure_marks_ondeck_incomplete():
    routes = make_routes()
    routes["/Shows/NextUp"] = FakeResponse(500, None)
    mgr = make_manager(routes)
    mgr.get_on_deck_media([], 30, 1, False, [])
    assert not mgr.is_ondeck_data_complete()


def test_active_sessions_prefer_playing_media_source():
    routes = make_routes()
    routes["/Sessions"] = [
        {"UserName": "kid", "PlayState": {"MediaSourceId": "src-4k"},
         "NowPlayingItem": {"Id": "m1", "Name": "Movie", "Type": "Movie", "MediaSources": [
             {"Id": "src-hd", "Protocol": "File", "Path": "/m-hd.mkv"},
             {"Id": "src-4k", "Protocol": "File", "Path": "/m-4k.mkv"}]}},
        {"UserName": "idle"},  # no NowPlayingItem
    ]
    mgr = make_manager(routes)
    sessions = mgr.get_active_sessions()
    assert sessions == [JellyfinSession(file_path="/m-4k.mkv", username="kid", title="Movie")]


def test_active_session_without_path_is_resolved_via_items():
    routes = make_routes()
    routes["/Sessions"] = [{"UserName": "kid", "UserId": "u-kid",
                            "NowPlayingItem": {"Id": "e5", "Name": "Ep 5", "SeriesName": "Show"}}]
    routes["/Items"] = {"Items": [episode("e5", 1, 5, path="/tv/e5.mkv")]}
    mgr = make_manager(routes)
    sessions = mgr.get_active_sessions()
    assert [(s.file_path, s.title) for s in sessions] == [("/tv/e5.mkv", "Show - Ep 5")]
