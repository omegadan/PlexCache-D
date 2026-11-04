"""Config loading for the media_server switch and Jellyfin settings."""

import json

import pytest

from core.config import ConfigManager


def base_settings(**overrides):
    settings = {
        "number_episodes": 5,
        "days_to_monitor": 30,
        "users_toggle": True,
        "watched_move": True,
        "cache_dir": "/mnt/cache/media/",
        "max_concurrent_moves_array": 2,
        "max_concurrent_moves_cache": 2,
        "path_mappings": [{
            "name": "Media", "plex_path": "/media", "real_path": "/mnt/user/media",
            "cache_path": "/mnt/cache/media", "cacheable": True, "enabled": True,
        }],
    }
    settings.update(overrides)
    return settings


def jellyfin_settings(**overrides):
    return base_settings(**dict({
        "media_server": "jellyfin",
        "JELLYFIN_URL": "http://jellyfin:8096",
        "JELLYFIN_API_KEY": "abc123",
    }, **overrides))


def load(tmp_path, settings):
    path = tmp_path / "plexcache_settings.json"
    path.write_text(json.dumps(settings))
    cm = ConfigManager(str(path))
    cm.load_config(persist_updates=False)
    return cm


def test_plex_is_default_and_still_requires_plex_fields(tmp_path):
    with pytest.raises(ValueError, match="PLEX_URL"):
        load(tmp_path, base_settings())


def test_jellyfin_loads_without_any_plex_or_watchlist_fields(tmp_path):
    cm = load(tmp_path, jellyfin_settings(
        jellyfin_user="dan", jellyfin_libraries=["Movies", "TV"],
    ))
    assert cm.media_server == "jellyfin" and cm.is_jellyfin
    assert cm.jellyfin.jellyfin_url == "http://jellyfin:8096"
    assert cm.jellyfin.jellyfin_api_key == "abc123"
    assert cm.jellyfin.jellyfin_user == "dan"
    assert cm.jellyfin.jellyfin_libraries == ["Movies", "TV"]
    assert cm.cache.watchlist_toggle is False


def test_jellyfin_forces_watchlist_off(tmp_path):
    cm = load(tmp_path, jellyfin_settings(
        watchlist_toggle=True, watchlist_episodes=3, remote_watchlist_toggle=True,
    ))
    assert cm.cache.watchlist_toggle is False
    assert cm.cache.remote_watchlist_toggle is False


def test_jellyfin_requires_url_and_key(tmp_path):
    with pytest.raises(ValueError, match="JELLYFIN_API_KEY"):
        load(tmp_path, jellyfin_settings(JELLYFIN_API_KEY="  "))


def test_unknown_media_server_rejected(tmp_path):
    with pytest.raises(ValueError, match="media_server"):
        load(tmp_path, jellyfin_settings(media_server="emby"))


def test_jellyfin_users_are_separate_from_plex_users(tmp_path):
    cm = load(tmp_path, jellyfin_settings(
        users=[{"title": "plexfriend", "token": "t", "skip_ondeck": True}],
        jellyfin_users=[
            {"title": "kid", "skip_ondeck": True},
            {"title": "partner", "days_to_monitor": 7},
        ],
    ))
    assert cm.users_settings_key == "jellyfin_users"
    assert [u["title"] for u in cm.plex.users] == ["kid", "partner"]
    assert cm.plex.skip_ondeck == ["kid"]
    assert cm.plex.per_user_ondeck_days == {"partner": 7}


def test_plex_mode_unchanged_with_jellyfin_keys_present(tmp_path):
    cm = load(tmp_path, base_settings(
        PLEX_URL="http://plex:32400", PLEX_TOKEN="tok", valid_sections=[1],
        watchlist_toggle=True, watchlist_episodes=3,
        JELLYFIN_URL="http://jellyfin:8096",
        users=[{"title": "friend"}], jellyfin_users=[{"title": "kid"}],
    ))
    assert cm.media_server == "plex"
    assert cm.cache.watchlist_toggle is True
    assert [u["title"] for u in cm.plex.users] == ["friend"]
