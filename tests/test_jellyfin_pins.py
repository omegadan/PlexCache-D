"""Pinned media on Jellyfin: the plexapi-shaped adapter and per-backend pin files."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.jellyfin_pins import JellyfinPinServer, _resolution_label
from core.pinned_media import (
    PinnedMediaTracker, estimate_item_size, fetch_pin_item, pinned_media_filename,
    resolve_pins_to_paths, select_media_version,
)

MOVIE_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
SHOW_ID = "5h0w"


def src(path, width, height, size, bitrate=8_000_000, protocol="File"):
    return {"Path": path, "Size": size, "Bitrate": bitrate, "Protocol": protocol,
            "MediaStreams": [{"Type": "Audio"}, {"Type": "Video", "Width": width, "Height": height}]}


def ep(n, season=1):
    return {"Id": f"e{season}{n}", "Name": f"Ep {n}", "Type": "Episode", "IndexNumber": n,
            "ParentIndexNumber": season, "SeasonId": f"s{season}", "SeriesId": SHOW_ID,
            "SeriesName": "Show", "MediaSources": [src(f"/data/tv/Show/S0{season}E0{n}.mkv", 1920, 1080, 100)]}


ITEMS = {
    MOVIE_ID: {"Id": MOVIE_ID, "Name": "Film", "Type": "Movie", "ProductionYear": 2020, "MediaSources": [
        src("/data/movies/Film - 1080p.mkv", 1920, 1080, 4_000),
        src("/data/movies/Film - 2160p.mkv", 3840, 2160, 20_000),
        src("http://stream/strm", 1920, 1080, 0, protocol="Http"),  # not a local file
    ]},
    SHOW_ID: {"Id": SHOW_ID, "Name": "Show", "Type": "Series", "ProductionYear": 2019},
    "s1": {"Id": "s1", "Name": "Season 1", "Type": "Season", "IndexNumber": 1, "SeriesId": SHOW_ID,
           "SeriesName": "Show", "ChildCount": 2},
    "e12": ep(2),
}
SEASONS = [ITEMS["s1"], {"Id": "s2", "Name": "Season 2", "Type": "Season", "IndexNumber": 2, "SeriesId": SHOW_ID}]
EPISODES = [ep(1), ep(2), ep(1, season=2)]


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self):
        self.headers, self.calls = {}, []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        path = url.split("8096", 1)[1]
        self.calls.append((path, params))
        if path == "/System/Info":
            return FakeResp(200, {"ServerName": "JF"})
        if path == "/Items" and "Ids" in params:
            item = ITEMS.get(params["Ids"])
            return FakeResp(200, {"Items": [item] if item else []})
        if path == "/Items" and "searchTerm" in params:
            want = params["IncludeItemTypes"]
            hits = [i for i in ITEMS.values() if i["Type"] == want and params["searchTerm"].lower() in i["Name"].lower()]
            return FakeResp(200, {"Items": hits})
        if path == f"/Shows/{SHOW_ID}/Seasons":
            return FakeResp(200, {"Items": SEASONS})
        if path == f"/Shows/{SHOW_ID}/Episodes":
            season = params.get("seasonId")
            return FakeResp(200, {"Items": [e for e in EPISODES if not season or e["SeasonId"] == season]})
        return FakeResp(404)


@pytest.fixture
def server():
    return JellyfinPinServer("http://jf:8096", "key", session=FakeSession())


@pytest.fixture
def tracker(tmp_path):
    return PinnedMediaTracker(str(tmp_path / "pinned_media_jellyfin.json"))


def test_resolution_labels():
    assert _resolution_label(3840, 2160) == "4k"
    assert _resolution_label(1920, 800) == "1080"  # scope crop
    assert _resolution_label(1280, 720) == "720"
    assert _resolution_label(720, 480) == "480"
    assert _resolution_label(None, None) == "sd"


def test_fetch_keeps_guid_string(server):
    item = fetch_pin_item(server, MOVIE_ID)
    assert item.ratingKey == MOVIE_ID and item.type == "movie" and item.year == 2020
    # Http sources are dropped; both local versions become Media entries
    assert [m.parts[0].file for m in item.media] == ["/data/movies/Film - 1080p.mkv", "/data/movies/Film - 2160p.mkv"]


def test_version_preference_applies_to_jellyfin_sources(server):
    item = server.fetchItem(MOVIE_ID)
    assert select_media_version(item, "highest").parts[0].file.endswith("2160p.mkv")
    assert select_media_version(item, "1080p").parts[0].file.endswith("1080p.mkv")
    assert estimate_item_size(item, "movie", "lowest") == 4_000


def test_resolve_show_season_episode_and_movie_pins(server, tracker):
    tracker.add_pin(SHOW_ID, "show", "Show")
    tracker.add_pin("s1", "season", "Show - Season 1")
    tracker.add_pin("e12", "episode", "Show - S01E02")
    tracker.add_pin(MOVIE_ID, "movie", "Film")
    resolved, orphaned = resolve_pins_to_paths(server, tracker, "1080p")
    by_key = {}
    for path, key, _type in resolved:
        by_key.setdefault(key, []).append(path)
    assert len(by_key[SHOW_ID]) == 3
    assert by_key["s1"] == ["/data/tv/Show/S01E01.mkv", "/data/tv/Show/S01E02.mkv"]
    assert by_key["e12"] == ["/data/tv/Show/S01E02.mkv"]
    assert by_key[MOVIE_ID] == ["/data/movies/Film - 1080p.mkv"]
    assert orphaned == []
    # Season episodes come from /Shows/{series}/Episodes?seasonId= (works for virtual seasons)
    assert (f"/Shows/{SHOW_ID}/Episodes", ) in [(c[0],) for c in server.http.calls if c[1].get("seasonId") == "s1"]


def test_deleted_item_is_orphaned_and_pruned_only_when_asked(server, tracker):
    tracker.add_pin("gone", "movie", "Deleted Film")
    _, orphaned = resolve_pins_to_paths(server, tracker)
    assert orphaned == ["gone"] and tracker.is_pinned("gone")
    _, orphaned = resolve_pins_to_paths(server, tracker, prune=True)
    assert orphaned == ["gone"] and not tracker.is_pinned("gone")


def test_unreachable_server_keeps_pins(tracker):
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("down")
    server = JellyfinPinServer("http://jf:8096", "key", session=session)
    tracker.add_pin(MOVIE_ID, "movie", "Film")
    with patch("core.pinned_media.time.sleep"):
        resolved, orphaned = resolve_pins_to_paths(server, tracker, prune=True)
    assert resolved == [] and orphaned == [] and tracker.is_pinned(MOVIE_ID)


def test_search_and_children_look_like_plexapi(server):
    assert [h.title for h in server.search("film", mediatype="movie")] == ["Film"]
    show = server.search("show", mediatype="show")[0]
    seasons = show.seasons()
    assert [s.title for s in seasons] == ["Season 1", "Season 2"]
    season = server.fetchItem("s1")
    assert (season.parentRatingKey, season.parentTitle, season.leafCount, season.index) == (SHOW_ID, "Show", 2, 1)
    episode = server.fetchItem("e12")
    assert (episode.grandparentRatingKey, episode.grandparentTitle, episode.parentIndex, episode.index) == (SHOW_ID, "Show", 1, 2)


def test_per_backend_pin_files(tmp_path):
    assert pinned_media_filename("plex") == "pinned_media.json"
    assert pinned_media_filename("jellyfin") == "pinned_media_jellyfin.json"
    from core.config import ConfigManager
    cm = object.__new__(ConfigManager)
    cm.paths = MagicMock(data_folder=str(tmp_path))
    with patch.object(ConfigManager, "media_server", "jellyfin", create=True):
        assert cm.get_pinned_media_file() == tmp_path / "pinned_media_jellyfin.json"


def test_jellyfin_manager_pin_server_shares_session():
    from core.jellyfin_api import JellyfinManager
    mgr = JellyfinManager("http://jf:8096/", "key")
    pin_server = mgr.get_pin_server()
    assert pin_server.http is mgr.http and pin_server.url == "http://jf:8096"
    assert pin_server.accepts_string_keys is True


def test_web_pinned_service_uses_jellyfin_server_and_pin_file(tmp_path):
    from web.services.pinned_service import PinnedService
    settings = MagicMock()
    settings.get_media_server.return_value = "jellyfin"
    settings.get_jellyfin_settings.return_value = {"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key"}
    settings.get_all.return_value = {"pinned_preferred_resolution": "highest"}
    with patch("web.services.get_settings_service", return_value=settings), \
         patch("web.dependencies.DATA_DIR", tmp_path):
        svc = PinnedService()
        assert isinstance(svc._get_plex_server(), JellyfinPinServer)
        svc._get_plex_server = lambda: JellyfinPinServer("http://jf:8096", "key", session=FakeSession())

        hits = svc.search("film")
        assert [(h["rating_key"], h["title"], h["size_bytes"]) for h in hits] == [(MOVIE_ID, "Film", 20_000)]
        children = svc.expand(SHOW_ID, "show")
        assert [c["title"] for c in children] == ["Show (2019) — Season 1", "Show (2019) — Season 2"]

        svc.get_tracker().add_pin(MOVIE_ID, "movie", "Film")
        assert (tmp_path / "pinned_media_jellyfin.json").exists()
        assert not (tmp_path / "pinned_media.json").exists()

        # Switching back to Plex shows the Plex pins again, untouched
        settings.get_media_server.return_value = "plex"
        assert not svc.is_pinned_rating_key(MOVIE_ID)
        settings.get_media_server.return_value = "jellyfin"
        assert svc.is_pinned_rating_key(MOVIE_ID)
