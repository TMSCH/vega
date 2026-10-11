"""Vega's own settings: the allowlist, how to find Mission Control, where fleet lives.

They live in a TOML file outside the repo (``vega.toml`` in Vega's Hermes profile by
default) so that no Slack id or machine path is ever committed. ``vega.example.toml``
at the repo root documents every key and its default.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROFILE = "vega"
DEFAULT_LEAD_MATCH = "Mission Control"
DEFAULT_REFUSAL = (
    "Sorry, I only take requests from people and channels on my allowlist. "
    "Ask the fleet operator to add you if you need access."
)
DEFAULT_ACK = "Passed to Mission Control. It will answer in this thread."
DEFAULT_STATUS_PATTERNS = (
    r"\bwhat'?s (running|in flight|going on|happening)\b",
    r"\bwhat is (running|in flight|going on|happening)\b",
    r"^\s*status\??\s*$",
    r"\b(fleet|queue) status\b",
    r"\bstatus of the (fleet|queue)\b",
    r"\bwhat'?s (in )?the queue\b",
)

ANY_CHANNEL = "*"
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{2,}$")


class ConfigError(ValueError):
    """The config file exists but says something Vega cannot use."""


@dataclass(frozen=True)
class VegaConfig:
    allowed_users: frozenset[str] = frozenset()
    allowed_channels: frozenset[str] = frozenset()
    refusal: str = DEFAULT_REFUSAL
    lead_match: str = DEFAULT_LEAD_MATCH
    ack: str = DEFAULT_ACK
    fleet_project: Path = field(default_factory=lambda: Path("~/fleet").expanduser())
    hermes_profile: str = DEFAULT_PROFILE
    runtime_dir: Path = field(
        default_factory=lambda: Path("~/.local/state/vega").expanduser()
    )
    max_inline_chars: int = 400
    dry_run: bool = False
    status_patterns: tuple[str, ...] = DEFAULT_STATUS_PATTERNS
    source: Path | None = None


def default_config_path() -> Path:
    """Where Vega looks when nothing names a file.

    ``$VEGA_CONFIG`` wins; inside the Hermes gateway ``$HERMES_HOME`` is Vega's profile;
    from a shell it falls back to the profile's usual home.
    """
    if env := os.environ.get("VEGA_CONFIG"):
        return Path(env).expanduser()
    if home := _hermes_home():
        return home / "vega.toml"
    return Path("~/.hermes/profiles").expanduser() / DEFAULT_PROFILE / "vega.toml"


def _hermes_home() -> Path | None:
    """The active profile's home inside Hermes (profile-scoped when multiplexed)."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        home = os.environ.get("HERMES_HOME")
        return Path(home).expanduser() if home else None
    return Path(get_hermes_home())


def load(path: Path | None = None) -> VegaConfig:
    """Read the config; a missing file is an empty allowlist, which refuses everyone."""
    path = path or default_config_path()
    if not path.is_file():
        return VegaConfig(source=None)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return from_dict(data, source=path)


def from_dict(data: dict, source: Path | None = None) -> VegaConfig:
    access = _table(data, "access")
    mc = _table(data, "mission_control")
    fleet = _table(data, "fleet")
    slack = _table(data, "slack")
    runtime = _table(data, "runtime")
    status = _table(data, "status")

    users = _ids(access.get("users", []), USER_ID_RE, "access.users")
    raw_channels = access.get("channels", [])
    if raw_channels == [ANY_CHANNEL]:
        # Any channel Vega has been invited to; Slack only delivers those.
        channels = frozenset({ANY_CHANNEL})
    else:
        channels = _ids(raw_channels, CHANNEL_ID_RE, "access.channels")
    max_inline = int(runtime.get("max_inline_chars", 400))
    if max_inline < 40:
        raise ConfigError("runtime.max_inline_chars must be at least 40")
    patterns = tuple(status.get("patterns", DEFAULT_STATUS_PATTERNS))
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"status.patterns: {pattern!r}: {exc}") from exc

    defaults = VegaConfig()
    return VegaConfig(
        allowed_users=users,
        allowed_channels=channels,
        refusal=str(access.get("refusal", DEFAULT_REFUSAL)),
        lead_match=str(mc.get("lead_match", DEFAULT_LEAD_MATCH)),
        ack=str(mc.get("ack", DEFAULT_ACK)),
        fleet_project=_path(fleet.get("project"), defaults.fleet_project),
        hermes_profile=str(slack.get("hermes_profile", DEFAULT_PROFILE)),
        runtime_dir=_path(runtime.get("dir"), defaults.runtime_dir),
        max_inline_chars=max_inline,
        dry_run=bool(runtime.get("dry_run", False)),
        status_patterns=patterns,
        source=source,
    )


def _table(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _ids(values: object, pattern: re.Pattern[str], key: str) -> frozenset[str]:
    if not isinstance(values, list):
        raise ConfigError(f"{key} must be a list of Slack ids")
    out = set()
    for value in values:
        text = str(value).strip()
        if not pattern.match(text):
            raise ConfigError(f"{key}: {text!r} does not look like a Slack id")
        out.add(text)
    return frozenset(out)


def _path(value: object, default: Path) -> Path:
    return Path(str(value)).expanduser() if value else default
