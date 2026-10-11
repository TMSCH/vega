from __future__ import annotations

from pathlib import Path

import pytest
from vega import cli
from vega import runner as runner_mod

from .conftest import CHANNEL, TS, USER


@pytest.fixture
def conf(tmp_path: Path) -> Path:
    path = tmp_path / "vega.toml"
    path.write_text(
        f'[access]\nusers = ["{USER}"]\nchannels = ["{CHANNEL}"]\n'
        f'[runtime]\ndir = "{tmp_path / "state"}"\n'
    )
    return path


def test_reply_dry_run(capsys, conf):
    assert (
        cli.main(
            ["--config", str(conf), "reply", f"{CHANNEL}/{TS}", "on it", "--dry-run"]
        )
        == 0
    )
    assert (
        capsys.readouterr().out
        == f"[dry-run] would post to slack:{CHANNEL}:{TS}:\non it\n"
    )


def test_reply_from_file_dry_run(capsys, conf, tmp_path):
    body = tmp_path / "answer.md"
    body.write_text("line 1\nline 2\n")
    assert (
        cli.main(
            [
                "--config",
                str(conf),
                "reply",
                f"{CHANNEL}/{TS}",
                "--file",
                str(body),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "line 1\nline 2" in capsys.readouterr().out


def test_reply_bad_target_is_a_usage_error(capsys, conf):
    assert cli.main(["--config", str(conf), "reply", "general", "x", "--dry-run"]) == 2
    assert "bad target" in capsys.readouterr().err


def test_relay_simulation(monkeypatch, capsys, conf, fake):
    monkeypatch.setattr(runner_mod, "run", fake)
    code = cli.main(
        [
            "--config",
            str(conf),
            "relay",
            "--user",
            USER,
            "--name",
            "Ada",
            "--channel",
            CHANNEL,
            "--thread-ts",
            TS,
            "ship it",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "outcome: relayed" in out
    assert fake.sent()[0][-1] == f"[from: Ada · slack: {CHANNEL}/{TS}] ship it"


def test_relay_simulation_refuses_stranger(monkeypatch, capsys, conf, fake):
    monkeypatch.setattr(runner_mod, "run", fake)
    assert (
        cli.main(
            [
                "--config",
                str(conf),
                "relay",
                "--user",
                "U0NOBODY0",
                "--channel",
                CHANNEL,
                "--thread-ts",
                TS,
                "ship it",
            ]
        )
        == 0
    )
    assert "outcome: refused" in capsys.readouterr().out
    assert fake.calls == []
