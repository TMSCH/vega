"""Vega's own settings: the allowlist, where fleet lives, how a thread polls its work.

They live in a TOML file outside the repo (``vega.toml`` in Vega's Hermes profile by
default) so that no Slack id or machine path is ever committed. ``vega.example.toml``
at the repo root documents every key and its default.

The schema is closed: a table or key Vega does not read is an error, not a warning.
A misspelt ``[acces]`` must not silently leave the allowlist empty, and a key from
an earlier design must not sit in the file looking as if it still did something.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROFILE = "vega"
DEFAULT_REFUSAL = (
    "Sorry, I only take requests from people and channels on my allowlist. "
    "Ask the fleet operator to add you if you need access."
)
DEFAULT_HEARTBEAT_INTERVAL = "5m"
MIN_HEARTBEAT_SECONDS = 60
DEFAULT_FUEL_MARGIN = 10
DEFAULT_MAX_SENDS_PER_HOUR = 6
DEFAULT_MAX_BRIEF_CHARS = 20_000

ANY_CHANNEL = "*"
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{2,}$")
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*(s|m|h)\s*$")

# Every table and key ``vega.toml`` may contain. Anything else is refused.
SCHEMA: dict[str, frozenset[str]] = {
    "access": frozenset({"users", "channels", "refusal"}),
    "fleet": frozenset({"project", "repos", "fuel_margin", "fuel_providers"}),
    "origin": frozenset(
        {"heartbeat_interval", "max_sends_per_hour", "max_brief_chars"}
    ),
    "runtime": frozenset({"dir"}),
}


class ConfigError(ValueError):
    """The config file exists but says something Vega cannot use."""


@dataclass(frozen=True)
class VegaConfig:
    allowed_users: frozenset[str] = frozenset()
    allowed_channels: frozenset[str] = frozenset()
    refusal: str = DEFAULT_REFUSAL
    fleet_project: Path = field(default_factory=lambda: Path("~/fleet").expanduser())
    # Repositories a task may name, as resolved paths. Empty means no task can be added.
    repos: tuple[Path, ...] = ()
    # Dispatch only when every gated provider reads at least reserve + margin, fresh.
    fuel_margin: int = DEFAULT_FUEL_MARGIN
    # Providers the gate reads; empty means the first account `fleet status --fuel`
    # prints, which is the checkout's own.
    fuel_providers: tuple[str, ...] = ()
    runtime_dir: Path = field(
        default_factory=lambda: Path("~/.local/state/vega").expanduser()
    )
    heartbeat_seconds: int = 300
    max_sends_per_hour: int = DEFAULT_MAX_SENDS_PER_HOUR
    max_brief_chars: int = DEFAULT_MAX_BRIEF_CHARS
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
    _check_schema(data)
    access = _table(data, "access")
    fleet = _table(data, "fleet")
    runtime = _table(data, "runtime")
    origin = _table(data, "origin")

    users = _ids(access.get("users", []), USER_ID_RE, "access.users")
    raw_channels = access.get("channels", [])
    if raw_channels == [ANY_CHANNEL]:
        # Any channel Vega has been invited to; Slack only delivers those.
        channels = frozenset({ANY_CHANNEL})
    else:
        channels = _ids(raw_channels, CHANNEL_ID_RE, "access.channels")

    defaults = VegaConfig()
    return VegaConfig(
        allowed_users=users,
        allowed_channels=channels,
        refusal=str(access.get("refusal", DEFAULT_REFUSAL)),
        fleet_project=_path(fleet.get("project"), defaults.fleet_project),
        repos=_paths(fleet.get("repos", []), "fleet.repos"),
        fuel_margin=_int(
            fleet.get("fuel_margin", DEFAULT_FUEL_MARGIN), "fleet.fuel_margin", 0
        ),
        fuel_providers=_names(fleet.get("fuel_providers", []), "fleet.fuel_providers"),
        runtime_dir=_path(runtime.get("dir"), defaults.runtime_dir),
        heartbeat_seconds=parse_interval(
            str(origin.get("heartbeat_interval", DEFAULT_HEARTBEAT_INTERVAL))
        ),
        max_sends_per_hour=_int(
            origin.get("max_sends_per_hour", DEFAULT_MAX_SENDS_PER_HOUR),
            "origin.max_sends_per_hour",
            1,
        ),
        max_brief_chars=_int(
            origin.get("max_brief_chars", DEFAULT_MAX_BRIEF_CHARS),
            "origin.max_brief_chars",
            500,
        ),
        source=source,
    )


def parse_interval(text: str) -> int:
    """``5m`` / ``90s`` / ``1h`` -> seconds, at least :data:`MIN_HEARTBEAT_SECONDS`."""
    match = _INTERVAL_RE.match(text)
    if not match:
        raise ConfigError(
            f"origin.heartbeat_interval: {text!r} is not an interval like 5m, 90s or 1h"
        )
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds < MIN_HEARTBEAT_SECONDS:
        raise ConfigError(
            f"origin.heartbeat_interval must be at least {MIN_HEARTBEAT_SECONDS}s"
        )
    return seconds


def _check_schema(data: dict) -> None:
    """Refuse any table or key outside :data:`SCHEMA`, naming the first one found."""
    for name in data:
        if name not in SCHEMA:
            raise ConfigError(
                f"[{name}] is not a Vega table; known: {', '.join(sorted(SCHEMA))}"
            )
    for name, keys in SCHEMA.items():
        for key in _table(data, name):
            if key not in keys:
                known = ", ".join(sorted(keys))
                raise ConfigError(f"{name}.{key} is not a Vega setting; known: {known}")


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


def _names(values: object, key: str) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ConfigError(f"{key} must be a list of names")
    out = []
    for value in values:
        text = str(value).strip()
        if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", text):
            raise ConfigError(f"{key}: {text!r} is not a provider name")
        out.append(text)
    return tuple(out)


def _paths(values: object, key: str) -> tuple[Path, ...]:
    if not isinstance(values, list):
        raise ConfigError(f"{key} must be a list of paths")
    return tuple(Path(str(v)).expanduser() for v in values if str(v).strip())


def _int(value: object, key: str, floor: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be a whole number") from exc
    if number < floor:
        raise ConfigError(f"{key} must be at least {floor}")
    return number


def _path(value: object, default: Path) -> Path:
    return Path(str(value)).expanduser() if value else default
