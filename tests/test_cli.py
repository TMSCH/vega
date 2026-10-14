from __future__ import annotations

from pathlib import Path

import pytest
from vega import cli
from vega import runner as runner_mod

from .conftest import CHANNEL, ORIGIN, TS, USER, FakeRunner, show_text


@pytest.fixture
def conf(tmp_path: Path) -> Path:
    path = tmp_path / "vega.toml"
    path.write_text(
        f'[access]\nusers = ["{USER}"]\nchannels = ["{CHANNEL}"]\n'
        f'[runtime]\ndir = "{tmp_path / "state"}"\n'
        f'[fleet]\nproject = "{tmp_path / "fleet"}"\n'
    )
    return path


def test_admit_records_the_thread_then_threads_and_news(monkeypatch, capsys, conf):
    fake = FakeRunner()
    monkeypatch.setattr(runner_mod, "run", fake)
    base = ["--config", str(conf)]
    code = cli.main(
        [
            *base,
            "admit",
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
    assert code == 0 and "outcome: admitted" in out and f"origin:  {ORIGIN}" in out
    assert cli.main([*base, "threads"]) == 0
    assert "1 message(s)" in capsys.readouterr().out
    assert cli.main([*base, "news", f"{CHANNEL}/{TS}"]) == 0
    assert capsys.readouterr().out == "no news\nopen: none (done)\n"
    assert fake.fleet_calls() == [["queue", "find", "--origin", ORIGIN, "--json"]]


def test_admit_refuses_a_stranger(capsys, conf):
    code = cli.main(
        [
            "--config",
            str(conf),
            "admit",
            "--user",
            "U0NOBODY0",
            "--channel",
            CHANNEL,
            "--thread-ts",
            TS,
            "ship it",
        ]
    )
    assert code == 1 and "outcome: refused" in capsys.readouterr().out


def test_status_show_and_config(monkeypatch, capsys, conf):
    fake = FakeRunner(shows={"t/01-a": show_text("t/01-a")})
    monkeypatch.setattr(runner_mod, "run", fake)
    assert cli.main(["--config", str(conf), "status", "fuel"]) == 0
    assert "provider\tclaude" in capsys.readouterr().out
    assert cli.main(["--config", str(conf), "show", "t/01-a"]) == 0
    assert "state:       queued" in capsys.readouterr().out
    assert cli.main(["--config", str(conf), "show", "nope"]) == 2
    assert cli.main(["--config", str(conf), "config"]) == 0
    out = capsys.readouterr().out
    assert "heartbeat every:  300s" in out and "fleet.repos is empty" in out
