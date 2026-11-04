"""Route test: walking the web setup wizard with Jellyfin as the media server.

Mounts the setup and auth routers on a minimal app backed by a real
SettingsService over a temp settings file. Jellyfin HTTP calls are answered by
a fake ``requests.get``; the resulting settings file must load in the core
ConfigManager as a Jellyfin config.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault('fcntl', MagicMock())
for _mod in [
    'apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
    'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
    'plexapi', 'plexapi.server', 'plexapi.myplex',
]:
    sys.modules.setdefault(_mod, MagicMock())


def _force_real_modules():
    for name in ["web.config", "web.routers", "web.routers.setup", "web.routers.auth",
                 "web.services", "web.services.settings_service", "web"]:
        mod = sys.modules.get(name)
        if isinstance(mod, MagicMock):
            del sys.modules[name]
    import web  # noqa: F401
    import web.config  # noqa: F401
    import web.routers.setup  # noqa: F401
    import web.services.settings_service  # noqa: F401


_force_real_modules()

JF = "http://jf:8096"

VIRTUAL_FOLDERS = [
    {"Name": "Movies", "ItemId": "lib-m", "CollectionType": "movies", "Locations": ["/data/movies"]},
    {"Name": "TV Shows", "ItemId": "lib-t", "CollectionType": "tvshows", "Locations": ["/data/tv"]},
    {"Name": "Music", "ItemId": "lib-x", "CollectionType": "music", "Locations": ["/data/music"]},
]
USERS = [
    {"Name": "kid", "Id": "u2", "Policy": {}},
    {"Name": "dan", "Id": "u1", "Policy": {"IsAdministrator": True}},
    {"Name": "partner", "Id": "u3", "Policy": {}},
    {"Name": "old", "Id": "u4", "Policy": {"IsDisabled": True}},
]


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def fake_get(url, headers=None, params=None, timeout=None):
    if (headers or {}).get("X-Emby-Token") != "good-key":
        return _Resp(401)
    path = url[len(JF):]
    routes = {
        "/System/Info": {"ServerName": "Home JF", "Version": "10.10.7"},
        "/Library/VirtualFolders": VIRTUAL_FOLDERS,
        "/Users": USERS,
    }
    return _Resp(200, routes[path]) if path in routes else _Resp(404)


@pytest.fixture
def env(tmp_path):
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    from web.routers import setup as setup_router
    from web.routers import auth as auth_router
    setup_router.clear_setup_state()
    with patch("web.services.settings_service.SETTINGS_FILE", settings_file), \
         patch("web.services.settings_service.DATA_DIR", tmp_path), \
         patch("requests.get", side_effect=fake_get):
        from web.services.settings_service import SettingsService
        svc = SettingsService()
        svc._cached_settings = None
        app = FastAPI()
        app.include_router(setup_router.router)
        app.include_router(auth_router.router, prefix="/auth")
        with patch("web.routers.setup.get_settings_service", return_value=svc):
            yield TestClient(app), svc, settings_file
    setup_router.clear_setup_state()


def _walk_to_step6(client):
    r = client.post("/setup/step2/jellyfin",
                    data={"jellyfin_url": JF + "/", "jellyfin_api_key": "good-key"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup?step=3"

    page = client.get("/setup?step=3").text
    assert "Movies" in page and "TV Shows" in page and "Music" not in page
    assert "Jellyfin Path:" in page and "Plex Path:" not in page

    r = client.post("/setup/step3", data={
        "cache_dir": "/mnt/cache",
        "libraries": ["lib-t"],
        "library_cacheable_lib-t": "on",
    }, follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/setup?step=4").text
    # admin is preselected as the main user; disabled users never offered
    assert 'value="dan" selected' in page
    assert "old" not in page

    r = client.post("/setup/step4", data={
        "jellyfin_user": "dan", "users_toggle": "on", "track_user": ["partner"],
    }, follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/setup?step=5").text
    assert "watchlist_toggle" not in page
    r = client.post("/setup/step5", data={
        "number_episodes": "4", "days_to_monitor": "60", "watched_move": "on",
        "cache_retention_hours": "12", "watchlist_toggle": "on",
    }, follow_redirects=False)
    assert r.status_code == 303


def test_full_jellyfin_wizard_writes_loadable_config(env, tmp_path):
    client, svc, settings_file = env
    from web.routers import setup as setup_router

    _walk_to_step6(client)
    page = client.get("/setup?step=6").text
    assert "Password Login" in page
    r = client.post("/setup/step6", data={
        "auth_enabled": "on", "auth_session_hours": "24",
        "auth_password_username": "admin", "auth_password": "s3cret",
    }, follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/setup?step=7").text
    assert "Jellyfin Server" in page and "2 user(s)" in page

    assert setup_router.is_setup_complete() is False  # nothing on disk yet
    r = client.post("/setup/complete", follow_redirects=False)
    assert r.status_code == 303
    assert setup_router.is_setup_complete() is True

    raw = json.loads(settings_file.read_text())
    assert raw["media_server"] == "jellyfin"
    assert raw["JELLYFIN_URL"] == JF and raw["JELLYFIN_API_KEY"] == "good-key"
    assert raw["jellyfin_libraries"] == ["TV Shows"]
    assert raw["jellyfin_user"] == "dan"
    users = {u["title"]: u["skip_ondeck"] for u in raw["jellyfin_users"]}
    assert users == {"kid": True, "partner": False}
    assert raw["watchlist_toggle"] is False
    assert raw["path_mappings"][0]["plex_path"] == "/data/tv/"
    assert raw["path_mappings"][0]["cache_path"] == "/mnt/cache/tv/"
    assert raw["auth_password_enabled"] is True and raw["auth_password_hash"]
    # Saved like the Plex wizard does; the cache browser reads it for user badges
    assert {u["title"]: u["is_admin"] for u in raw["_cached_users"]}["dan"] is True

    # The file the wizard wrote must load as a Jellyfin config in core
    from core.config import ConfigManager
    cm = ConfigManager(str(settings_file))
    cm.load_config(persist_updates=False)
    assert cm.is_jellyfin
    assert cm.jellyfin.jellyfin_libraries == ["TV Shows"]
    assert cm.plex.skip_ondeck == ["kid"]
    assert cm.plex.number_episodes == 4


def test_bad_api_key_stays_on_step2_with_error(env):
    client, _svc, _f = env
    r = client.post("/setup/step2/jellyfin", data={"jellyfin_url": JF, "jellyfin_api_key": "nope"})
    assert r.status_code == 200
    assert "rejected the API key" in r.text
    assert 'value="nope"' in r.text  # entered values kept


def test_non_http_url_rejected(env):
    client, _svc, _f = env
    r = client.post("/setup/test-jellyfin", data={"jellyfin_url": "file:///etc", "jellyfin_api_key": "good-key"})
    assert r.json()["success"] is False


def test_test_connection_endpoint(env):
    client, _svc, _f = env
    r = client.post("/setup/test-jellyfin", data={"jellyfin_url": JF, "jellyfin_api_key": "good-key"})
    assert r.json() == {"success": True, "server_name": "Home JF", "version": "10.10.7"}


def test_login_requires_password_for_jellyfin(env):
    client, _svc, _f = env
    _walk_to_step6(client)
    r = client.post("/setup/step6", data={"auth_enabled": "on"}, follow_redirects=False)
    assert r.status_code == 200 and "password login is the only way" in r.text


def test_switching_back_to_plex_clears_jellyfin_caches(env):
    client, _svc, _f = env
    from web.routers import setup as setup_router
    client.post("/setup/step2/jellyfin", data={"jellyfin_url": JF, "jellyfin_api_key": "good-key"})
    client.get("/setup?step=3")
    assert "_cached_libraries" in setup_router.get_setup_state()
    with patch("plexapi.server.PlexServer") as plex_server:
        plex_server.return_value.friendlyName = "Plex"
        r = client.post("/setup/step2", data={"plex_url": "http://p:32400", "plex_token": "t"},
                        follow_redirects=False)
    assert r.status_code == 303
    state = setup_router.get_setup_state()
    assert state["media_server"] == "plex"
    assert "_cached_libraries" not in state


def test_login_page_hides_plex_button_for_jellyfin(env):
    client, svc, _f = env
    raw = svc._load_raw()
    raw.update({"media_server": "jellyfin", "auth_enabled": True, "auth_password_enabled": True})
    svc._save_raw(raw)
    from web.services.auth_service import AuthService
    auth = AuthService()
    with patch("web.routers.auth.get_auth_service", return_value=auth), \
         patch.object(auth, "_load_settings", return_value=raw), \
         patch.object(auth, "is_auth_enabled", return_value=True):
        page = client.get("/auth/login").text
    assert 'name="password"' in page
    assert 'id="plex-oauth-btn"' not in page
