"""Route test: the Jellyfin settings tab saves backend selection and users."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault('fcntl', MagicMock())
for _mod in [
    'apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
    'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
    'plexapi', 'plexapi.server',
]:
    sys.modules.setdefault(_mod, MagicMock())


def _force_real_modules():
    for name in ["web.config", "web.routers", "web.routers.settings", "web.routers.setup",
                 "web.services", "web.services.settings_service", "web"]:
        mod = sys.modules.get(name)
        if isinstance(mod, MagicMock):
            del sys.modules[name]
    import web  # noqa: F401
    import web.config  # noqa: F401
    import web.routers.settings  # noqa: F401
    import web.services.settings_service  # noqa: F401


_force_real_modules()

SERVER_USERS = [
    {"Name": "admin", "Policy": {"IsAdministrator": True}},
    {"Name": "kid", "Policy": {}},
    {"Name": "gone", "Policy": {"IsDisabled": True}},
]


@pytest.fixture
def service(tmp_path):
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text(json.dumps({
        "cache_dir": "/mnt/cache",
        "jellyfin_users": [{"title": "partner", "skip_ondeck": False, "days_to_monitor": 7}],
    }), encoding="utf-8")
    with patch("web.services.settings_service.SETTINGS_FILE", settings_file), \
         patch("web.services.settings_service.DATA_DIR", tmp_path):
        from web.services.settings_service import SettingsService
        svc = SettingsService()
        svc._cached_settings = None
        yield svc


@pytest.fixture
def client(service):
    from web.routers import settings as settings_router
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/settings")
    with patch("web.routers.settings.get_settings_service", return_value=service):
        yield TestClient(app), service


def test_get_lists_saved_and_server_users(client):
    test_client, service = client
    service.save_jellyfin_settings({"media_server": "jellyfin",
                                    "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "k"})
    with patch.object(service, "check_jellyfin_connection",
                      return_value=(True, "", {}, SERVER_USERS)):
        r = test_client.get("/settings/jellyfin")
    assert r.status_code == 200
    body = r.text
    assert 'value="partner"' in body and 'value="kid"' in body and 'value="admin"' in body
    assert 'value="gone"' not in body


def test_put_saves_backend_connection_and_tracked_users(client):
    test_client, service = client
    r = test_client.put("/settings/jellyfin", data={
        "media_server": "jellyfin",
        "jellyfin_url": "http://jf:8096/",
        "jellyfin_api_key": " key ",
        "jellyfin_user": "admin",
        "jellyfin_libraries": "Movies, TV Shows, ",
        "users_toggle": "on",
        "known_user": ["partner", "kid"],
        "track_user": ["kid"],
    })
    assert r.status_code == 200 and "saved" in r.text
    raw = service._load_raw()
    assert raw["media_server"] == "jellyfin"
    assert raw["JELLYFIN_URL"] == "http://jf:8096"
    assert raw["JELLYFIN_API_KEY"] == "key"
    assert raw["jellyfin_libraries"] == ["Movies", "TV Shows"]
    assert raw["users_toggle"] is True
    users = {u["title"]: u for u in raw["jellyfin_users"]}
    assert users["kid"]["skip_ondeck"] is False
    assert users["partner"]["skip_ondeck"] is True
    assert users["partner"]["days_to_monitor"] == 7  # extras preserved


def test_put_rejects_non_http_url(client):
    test_client, service = client
    r = test_client.put("/settings/jellyfin", data={"media_server": "jellyfin",
                                                    "jellyfin_url": "file:///etc/passwd"})
    assert "http" in r.text
    assert "JELLYFIN_URL" not in service._load_raw()


def test_setup_complete_for_jellyfin(service):
    from web.routers import setup
    with patch("web.routers.setup.get_settings_service", return_value=service):
        assert setup.is_setup_complete() is False
        service.save_jellyfin_settings({"media_server": "jellyfin",
                                        "jellyfin_url": "http://jf", "jellyfin_api_key": "k"})
        assert setup.is_setup_complete() is True


def test_dashboard_connection_check_uses_jellyfin(service):
    service.save_jellyfin_settings({"media_server": "jellyfin",
                                    "jellyfin_url": "http://jf", "jellyfin_api_key": "k"})
    with patch.object(service, "check_jellyfin_connection", return_value=(True, "", {}, [])) as check:
        assert service.check_plex_connection() is True
        check.assert_called_once()
