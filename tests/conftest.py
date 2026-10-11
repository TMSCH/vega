from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from vega import config
from vega.runner import Completed

USER = "U0TESTUSER1"
OTHER_USER = "U0STRANGER1"
CHANNEL = "C0TESTCHAN1"
OTHER_CHANNEL = "C0OTHERCHAN"
DM = "D0TESTDM001"
TS = "1700000000.000100"


@dataclass
class FakeRunner:
    """Records every argv and answers from a script keyed by the command's start."""

    sessions: list[dict] = field(default_factory=list)
    send_rc: int = 0
    hermes_rc: int = 0
    fleet_out: str = "fleet status\n  1 topic"
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def __call__(self, argv, *, input=None, timeout=30.0, drop_env=()):
        argv = list(argv)
        self.calls.append((argv, input))
        self.dropped = tuple(drop_env)
        if argv[:3] == ["thurbox-cli", "session", "list"]:
            return Completed(0, json.dumps(self.sessions), "")
        if argv[:3] == ["thurbox-cli", "message", "send"]:
            return Completed(self.send_rc, "", "" if self.send_rc == 0 else "boom")
        if argv[:1] == ["hermes"]:
            return Completed(self.hermes_rc, "", "" if self.hermes_rc == 0 else "nope")
        if argv[:2] == ["uv", "run"]:
            return Completed(0, self.fleet_out, "")
        raise AssertionError(f"unexpected command {argv}")

    def sent(self) -> list[list[str]]:
        return [a for a, _ in self.calls if a[:3] == ["thurbox-cli", "message", "send"]]


LEAD = {"id": "lead-uuid", "name": "📡 Mission Control", "stopped": False}


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner(sessions=[LEAD, {"id": "w1", "name": "🚀 some worker"}])


@pytest.fixture
def cfg(tmp_path: Path) -> config.VegaConfig:
    return config.from_dict(
        {
            "access": {"users": [USER], "channels": [CHANNEL]},
            "runtime": {"dir": str(tmp_path / "state"), "max_inline_chars": 60},
            "fleet": {"project": str(tmp_path / "fleet")},
        }
    )
