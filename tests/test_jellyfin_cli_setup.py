"""CLI setup wizard (core/setup.py) with Jellyfin as the media server.

Prompts are answered by matching on prompt text (anything unmatched takes the
default by answering ""). Jellyfin HTTP calls go to a fake ``requests.get``.
The written settings file must load in ConfigManager as a Jellyfin config.
"""

import json
from unittest.mock import patch

import pytest

from tests.test_jellyfin_setup_wizard import JF, fake_get

import core.setup as cli


def answer_with(answers):
    """input() replacement: first answer whose key is in the prompt wins."""
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        if len(asked) > 200:
            raise AssertionError("prompt loop; last prompts: %r" % asked[-5:])
        for key, value in answers.items():
            if key in prompt:
                if isinstance(value, list):
                    return value.pop(0) if value else ""
                return value
        return ""
    fake_input.asked = asked
    return fake_input


@pytest.fixture
def run(tmp_path):
    settings_file = tmp_path / "plexcache_settings.json"

    def _run(answers, existing=None):
        if existing is not None:
            settings_file.write_text(json.dumps(existing))
        fake_input = answer_with(answers)
        with patch.object(cli, "settings_filename", str(settings_file)), \
             patch.object(cli, "settings_data", {}), \
             patch("builtins.input", side_effect=fake_input), \
             patch("requests.get", side_effect=fake_get), \
             patch("subprocess.run") as sub:
            cli.run_setup()
            assert not sub.called  # dry-run declined
        return json.loads(settings_file.read_text()), fake_input.asked
    return _run, settings_file


BASE_ANSWERS = {
    "Select option [1/2]": "2",
    "Jellyfin server address": JF,
    "Jellyfin API key": ["bad-key", "good-key"],
    "Cache drive path": "/mnt/cache",
    "Include?": ["n", "y"],          # Movies skipped, TV Shows included
    "Real path": "/mnt/user/tv/",
    "Main user": "dan",
    "other users?": "y",
    "Track kid?": "n",
    "Track partner?": "y",
    "Run a test": "n",
}


def test_fresh_cli_setup_writes_loadable_jellyfin_config(run):
    _run, settings_file = run
    raw, asked = _run(dict(BASE_ANSWERS))

    assert raw["media_server"] == "jellyfin"
    assert raw["JELLYFIN_URL"] == JF and raw["JELLYFIN_API_KEY"] == "good-key"
    assert raw["jellyfin_libraries"] == ["TV Shows"]
    assert raw["jellyfin_user"] == "dan"
    assert {u["title"]: u["skip_ondeck"] for u in raw["jellyfin_users"]} == {"kid": True, "partner": False}
    assert raw["users_toggle"] is True
    assert raw["watchlist_toggle"] is False and raw["remote_watchlist_toggle"] is False
    m = raw["path_mappings"][0]
    assert (m["plex_path"], m["real_path"], m["cache_path"]) == ("/data/tv/", "/mnt/user/tv/", "/mnt/cache/tv/")
    # Plex-only questions never asked; the bad key was retried
    assert not any("watchlist" in p.lower() for p in asked)
    assert not any("Plex" in p for p in asked)
    assert sum("Jellyfin API key" in p for p in asked) == 2

    from core.config import ConfigManager
    cm = ConfigManager(str(settings_file))
    cm.load_config(persist_updates=False)
    assert cm.is_jellyfin
    assert cm.jellyfin.jellyfin_user == "dan"
    assert cm.plex.skip_ondeck == ["kid"]


def test_plex_choice_still_runs_plex_flow(run):
    _run, _f = run
    # "1" = Plex as the server, then "1" = Plex.tv sign-in, where we stop.
    answers = {"Select option [1/2]": ["1", "1"], "Plex server address": "http://p:32400"}
    with patch.object(cli, "plex_oauth_authenticate", side_effect=KeyboardInterrupt) as oauth:
        with pytest.raises(KeyboardInterrupt):
            _run(answers)
    assert oauth.called


def test_existing_jellyfin_config_refreshes_users_without_plex_prompts(run):
    _run, _f = run
    existing = {
        "firststart": False, "media_server": "jellyfin",
        "JELLYFIN_URL": JF, "JELLYFIN_API_KEY": "good-key", "jellyfin_user": "dan",
        "jellyfin_users": [{"title": "partner", "id": "u3", "skip_ondeck": False, "skip_watchlist": True},
                           {"title": "gone", "id": "u9", "skip_ondeck": False, "skip_watchlist": True}],
        "path_mappings": [{"name": "TV", "plex_path": "/data/tv/", "real_path": "/mnt/user/tv/"}],
    }
    # Fill every "missing setting" so the full wizard isn't re-run
    for key in cli.check_for_missing_settings({}):
        existing.setdefault(key, "")
    raw, asked = _run({"refresh the user list": "y"}, existing=existing)

    assert not any("Plex" in p for p in asked)
    users = {u["title"]: u["skip_ondeck"] for u in raw["jellyfin_users"]}
    # partner keeps tracking, kid is new and untracked, removed user dropped, main excluded
    assert users == {"partner": False, "kid": True}
