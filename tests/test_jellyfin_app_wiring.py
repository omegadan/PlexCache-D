"""PlexCacheApp picks the Jellyfin backend and handles its sessions/pins."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.app import PlexCacheApp
from core.config import JellyfinConfig
from core.jellyfin_api import JellyfinManager, JellyfinSession
from core.plex_api import PlexManager


def make_app(media_server):
    app = object.__new__(PlexCacheApp)
    app.config_manager = MagicMock()
    app.config_manager.media_server = media_server
    app.config_manager.jellyfin = JellyfinConfig(
        jellyfin_url="http://jf:8096", jellyfin_api_key="k",
        jellyfin_user="dan", jellyfin_libraries=["TV"],
    )
    app.config_manager.performance = SimpleNamespace(retry_limit=3, delay=5)
    app.config_manager.get_user_tokens_file.return_value = "/tmp/tokens.json"
    app.config_manager.get_rss_cache_file.return_value = "/tmp/rss.json"
    return app


def test_jellyfin_backend_selected():
    app = make_app("jellyfin")
    app._init_plex_manager()
    assert isinstance(app.plex_manager, JellyfinManager)
    assert app.plex_manager.main_user_name == "dan"
    assert app.plex_manager.libraries == ["TV"]


def test_plex_backend_still_default():
    app = make_app("plex")
    app.config_manager.plex = SimpleNamespace(plex_url="http://p", plex_token="t", plex_db_path="")
    app.config_manager.cache = SimpleNamespace(watchlist_toggle=False)
    app._init_plex_manager()
    assert isinstance(app.plex_manager, PlexManager)


def test_jellyfin_session_path_used_directly():
    app = make_app("jellyfin")
    session = JellyfinSession(file_path="/media/tv/e1.mkv", username="kid", title="Show - Ep 1")
    assert app._get_media_path_from_session(session) == "/media/tv/e1.mkv"


def test_pins_skipped_in_jellyfin_mode():
    app = make_app("jellyfin")
    app.pinned_tracker = MagicMock()
    app.pinned_tracker.list_pins.return_value = [{"rating_key": "123"}]
    modified = {}
    app._process_pinned_media(modified)
    assert modified == {}
    assert app.pinned_items == set()
