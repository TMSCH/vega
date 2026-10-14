from __future__ import annotations

import re
from pathlib import Path

import pytest
from vega import config
from vega.access import decide

from .conftest import CHANNEL, OTHER_CHANNEL, OTHER_USER, USER


def test_missing_file_refuses_everyone(tmp_path: Path):
    cfg = config.load(tmp_path / "absent.toml")
    assert cfg.allowed_users == frozenset()
    assert not decide(cfg, user_id=USER, channel_id=CHANNEL, is_dm=True).allowed


def test_loads_toml(tmp_path: Path):
    path = tmp_path / "vega.toml"
    path.write_text(
        f'[access]\nusers = ["{USER}"]\nchannels = ["{CHANNEL}"]\n'
        '[fleet]\nproject = "~/f"\nrepos = ["~/app", "/srv/web"]\n'
        'fuel_margin = 15\nfuel_providers = ["claude"]\n'
        '[origin]\nheartbeat_interval = "90s"\nmax_sends_per_hour = 3\n'
        '[runtime]\ndir = "~/state"\n'
    )
    cfg = config.load(path)
    assert cfg.allowed_users == {USER}
    assert cfg.allowed_channels == {CHANNEL}
    assert cfg.fleet_project == Path("~/f").expanduser()
    assert cfg.repos == (Path("~/app").expanduser(), Path("/srv/web"))
    assert (cfg.fuel_margin, cfg.fuel_providers) == (15, ("claude",))
    assert (cfg.heartbeat_seconds, cfg.max_sends_per_hour) == (90, 3)
    assert cfg.runtime_dir == Path("~/state").expanduser()
    assert cfg.source == path


def test_defaults():
    cfg = config.from_dict({})
    assert cfg.heartbeat_seconds == 300
    assert cfg.fuel_margin == 10 and cfg.fuel_providers == ()
    assert cfg.repos == () and cfg.max_sends_per_hour == 6
    assert cfg.max_brief_chars == 20_000


@pytest.mark.parametrize(
    "data",
    [
        {"access": {"users": ["not-an-id"]}},
        {"access": {"channels": ["U0123ABC"]}},
        {"access": {"users": "U0123ABC"}},
        {"access": []},
        {"origin": {"heartbeat_interval": "30s"}},
        {"origin": {"heartbeat_interval": "soon"}},
        {"origin": {"max_sends_per_hour": 0}},
        {"origin": {"max_brief_chars": 10}},
        {"fleet": {"fuel_margin": -1}},
        {"fleet": {"fuel_providers": ["bad name"]}},
        {"fleet": {"repos": "~/app"}},
    ],
)
def test_rejects_bad_config(data):
    with pytest.raises(config.ConfigError):
        config.from_dict(data)


@pytest.mark.parametrize(
    ("data", "named"),
    [
        # A typo must not silently leave the allowlist empty.
        ({"acces": {"users": [USER]}}, "[acces]"),
        ({"access": {"user": [USER]}}, "access.user"),
        # Tables and keys from earlier designs are refused, not ignored.
        ({"mission_control": {"lead_match": "Lead"}}, "[mission_control]"),
        ({"status": {"patterns": []}}, "[status]"),
        ({"slack": {"hermes_profile": "vega"}}, "[slack]"),
        ({"runtime": {"dry_run": True}}, "runtime.dry_run"),
    ],
)
def test_rejects_unknown_tables_and_keys(data, named):
    with pytest.raises(config.ConfigError, match=re.escape(named)):
        config.from_dict(data)


def test_schema_matches_the_dataclass():
    """Every schema key is read by from_dict; the example file exercises them all."""
    example = Path(__file__).parents[1] / "vega.example.toml"
    text = example.read_text()
    for table, keys in config.SCHEMA.items():
        assert f"[{table}]" in text
        for key in keys:
            assert re.search(rf"^#? ?{key} = ", text, re.M), f"{table}.{key}"


def test_example_config_parses_and_names_no_ids():
    example = Path(__file__).parents[1] / "vega.example.toml"
    cfg = config.load(example)
    assert cfg.allowed_users == frozenset()
    assert cfg.allowed_channels == frozenset()
    assert cfg.repos == ()


def test_any_channel_vega_is_in(tmp_path: Path):
    cfg = config.from_dict({"access": {"users": [USER], "channels": ["*"]}})
    assert decide(cfg, user_id=USER, channel_id=OTHER_CHANNEL, is_dm=False).allowed
    assert not decide(cfg, user_id=OTHER_USER, channel_id=CHANNEL, is_dm=False).allowed
    assert not decide(cfg, user_id=USER, channel_id="", is_dm=False).allowed
    with pytest.raises(config.ConfigError):
        config.from_dict({"access": {"channels": ["*", CHANNEL]}})


def test_vega_config_env_wins(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEGA_CONFIG", str(tmp_path / "x.toml"))
    assert config.default_config_path() == tmp_path / "x.toml"


@pytest.mark.parametrize(
    ("user", "channel", "is_dm", "is_bot", "allowed"),
    [
        (USER, CHANNEL, False, False, True),
        (USER, "D0SOMEDM", True, False, True),
        (USER, OTHER_CHANNEL, False, False, False),
        (OTHER_USER, CHANNEL, False, False, False),
        (OTHER_USER, "D0SOMEDM", True, False, False),
        (USER, CHANNEL, False, True, False),
        ("", CHANNEL, False, False, False),
        (None, None, True, False, False),
    ],
)
def test_allowlist(cfg, user, channel, is_dm, is_bot, allowed):
    decision = decide(cfg, user_id=user, channel_id=channel, is_dm=is_dm, is_bot=is_bot)
    assert decision.allowed is allowed
    assert decision.reason
