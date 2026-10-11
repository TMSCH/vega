from __future__ import annotations

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
        '[mission_control]\nlead_match = "Lead"\n'
        "[runtime]\ndry_run = true\n"
    )
    cfg = config.load(path)
    assert cfg.allowed_users == {USER}
    assert cfg.allowed_channels == {CHANNEL}
    assert cfg.lead_match == "Lead"
    assert cfg.dry_run is True
    assert cfg.source == path


@pytest.mark.parametrize(
    "data",
    [
        {"access": {"users": ["not-an-id"]}},
        {"access": {"channels": ["U0123ABC"]}},
        {"access": {"users": "U0123ABC"}},
        {"access": []},
        {"runtime": {"max_inline_chars": 5}},
        {"status": {"patterns": ["("]}},
    ],
)
def test_rejects_bad_config(data):
    with pytest.raises(config.ConfigError):
        config.from_dict(data)


def test_example_config_parses_and_names_no_ids():
    example = Path(__file__).parents[1] / "vega.example.toml"
    cfg = config.load(example)
    assert cfg.allowed_users == frozenset()
    assert cfg.allowed_channels == frozenset()


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
