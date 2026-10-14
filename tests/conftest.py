from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest
from vega import config
from vega.runner import Completed

USER = "U0TESTUSER1"
OTHER_USER = "U0STRANGER1"
CHANNEL = "C0TESTCHAN1"
OTHER_CHANNEL = "C0OTHERCHAN"
DM = "D0TESTDM001"
TS = "1700000000.000100"
ORIGIN = f"slack:{CHANNEL}/{TS}"

FUEL_OK = (
    "read_at\t1\nreserve\t20\nremaining\t79\nstale\t0\nstate\tfresh\nprovider\tclaude\n"
    "\n"
    "read_at\t1\nreserve\t20\nremaining\t100\nstale\t0\nstate\tfresh\nprovider\tcodex\n"
    "\n"
    "read_at\t1\nreserve\t20\nremaining\t0\nstale\t0\nstate\tfresh\nprovider\tcopilot\n"
)
FUEL_LOW = FUEL_OK.replace("remaining\t79", "remaining\t27")


def show_text(
    ref: str,
    state: str = "queued",
    outcome: str = "",
    artifact: str = "",
    abandoned: str = "",
    brief: str = "",
    title: str = "Fix the thing",
    result: str = "",
) -> str:
    lines = [
        f"{ref} — {title}",
        f"    state:       {state}",
        "    repo:        /home/someone/app",
        "    host:        None",
        "    branch:      fix/thing",
        f"    outcome:     {outcome or 'None'}",
        "    by:          Ada",
        f"    origin:      {ORIGIN}",
    ]
    if artifact:
        lines.append(f"    artifact:    {artifact}")
    if abandoned:
        lines.append(f"    abandoned:   {abandoned}")
    if brief:
        lines.append(f"    brief:       {brief}")
    if result:
        lines.append(f"    result:      {result}")
    return "\n".join(lines) + "\n"


@dataclass
class FakeRunner:
    """Records every argv and answers from a script keyed by the fleet verb."""

    fleet_out: str = "fleet status\n  1 topic"
    fuel_out: str = FUEL_OK
    find_rows: list[dict] = field(default_factory=list)
    shows: dict[str, str] = field(default_factory=dict)
    rc: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    dropped: tuple = ()
    agents: set[str] = field(default_factory=lambda: {"claude-coder"})

    def __call__(self, argv, *, input=None, timeout=30.0, drop_env=()):
        argv = list(argv)
        self.calls.append((argv, input))
        self.dropped = tuple(drop_env)
        if argv[0].endswith("thurbox-cli"):
            assert argv[1:3] == ["agent", "launch-args"] and argv[4:] == ["--json"]
            if argv[3] in self.agents:
                return Completed(0, json.dumps({"agent": argv[3]}), "")
            return Completed(1, json.dumps({"error": f"no agent {argv[3]!r}"}), "")
        assert argv[:5] == ["uv", "run", "--project", argv[3], "fleet"], argv
        verb = " ".join(argv[5:7]) if argv[5] == "queue" else argv[5]
        if verb == "queue topic":
            verb = "queue topic add"
        code = self.rc.get(verb, 0)
        if code:
            return Completed(code, "", f"{verb} refused")
        if argv[5:] == ["status", "--fuel"]:
            return Completed(0, self.fuel_out, "")
        if verb in {"status", "context", "queue list", "queue plan"}:
            return Completed(0, self.fleet_out, "")
        if verb == "queue find":
            return Completed(0, json.dumps(self.find_rows), "")
        if verb == "queue show":
            ref = argv[7]
            if ref not in self.shows:
                return Completed(1, "", f"queue: no such task: {ref!r}")
            return Completed(0, self.shows[ref], "")
        if verb == "queue topic add":
            return Completed(0, f"topic {argv[8]} opened\n", "")
        if verb == "queue add":
            return Completed(0, f"{argv[7]}/01-{argv[8]}\n", "")
        if verb == "queue dispatch":
            return Completed(0, "dispatched 1 task\n", "")
        if verb == "queue send":
            return Completed(0, f"send: {argv[7]} -> sid  delivered\n", "")
        raise AssertionError(f"unexpected command {argv}")

    def fleet_calls(self) -> list[list[str]]:
        return [a[5:] for a, _ in self.calls if a[:2] == ["uv", "run"]]

    def thurbox_calls(self) -> list[list[str]]:
        return [a[1:] for a, _ in self.calls if a[0].endswith("thurbox-cli")]


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner()


THURBOX_CLI = "/opt/thurbox/bin/thurbox-cli"


@pytest.fixture
def thurbox_cli(monkeypatch) -> str:
    """Put a (fake) thurbox-cli on PATH, as far as ``shutil.which`` can tell."""
    monkeypatch.setattr(
        "vega.fleet.shutil.which",
        lambda name: THURBOX_CLI if name == "thurbox-cli" else None,
    )
    return THURBOX_CLI


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "app"
    path.mkdir()
    return path


@pytest.fixture
def cfg(tmp_path: Path, repo: Path) -> config.VegaConfig:
    return config.from_dict(
        {
            "access": {"users": [USER], "channels": [CHANNEL]},
            "runtime": {"dir": str(tmp_path / "state")},
            "fleet": {"project": str(tmp_path / "fleet"), "repos": [str(repo)]},
            "origin": {"heartbeat_interval": "5m", "max_sends_per_hour": 2},
        }
    )


class FakeHeartbeatState:
    def __init__(self, prompt, interval_seconds):
        self.prompt = prompt
        self.interval_seconds = interval_seconds
        self.status = "active"


class FakeHeartbeatManager:
    """Stands in for hermes_cli.heartbeat.HeartbeatManager, keyed by session id."""

    store: ClassVar[dict[str, FakeHeartbeatState]] = {}

    def __init__(self, session_id):
        self.session_id = session_id

    def set(self, prompt, interval_seconds):
        if interval_seconds < 60:
            raise ValueError("interval must be at least 60s")
        state = FakeHeartbeatState(prompt, interval_seconds)
        self.store[self.session_id] = state
        return state

    def is_active(self):
        state = self.store.get(self.session_id)
        return bool(state and state.status == "active")

    def clear(self):
        return self.store.pop(self.session_id, None) is not None

    def status_line(self):
        state = self.store.get(self.session_id)
        return (
            f"heartbeat every {state.interval_seconds}s" if state else "No heartbeat."
        )


@pytest.fixture
def fake_hermes_heartbeat(monkeypatch):
    """Install a fake ``hermes_cli.heartbeat`` so the plugin's arming path runs."""
    FakeHeartbeatManager.store = {}
    pkg = types.ModuleType("hermes_cli")
    mod = types.ModuleType("hermes_cli.heartbeat")
    mod.HeartbeatManager = FakeHeartbeatManager
    pkg.heartbeat = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.heartbeat", mod)
    return FakeHeartbeatManager


class FakeSessionStore:
    def __init__(self, routes: dict[str, str]):
        self.routes = routes

    def peek_session_id(self, key):
        return self.routes.get(key)
