"""Read-only views of fleet, for answering "what's running?" without waking the lead.

Only the argv tuples below are ever run. Nothing from Slack reaches an argv, and none of
them writes to fleet's queue: writes go through Mission Control.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import runner as _runner
from .config import VegaConfig
from .runner import Runner

VIEWS: dict[str, tuple[str, ...]] = {
    "status": ("fleet", "status"),
    "queue": ("fleet", "queue", "list", "--live"),
    "context": ("fleet", "context"),
}
MAX_OUTPUT_CHARS = 3500
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def argv(view: str, project: Path) -> list[str]:
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; choose one of {', '.join(VIEWS)}")
    return ["uv", "run", "--project", str(project), *VIEWS[view]]


def read_view(view: str, cfg: VegaConfig, runner: Runner | None = None) -> str:
    """Run one read-only fleet command and return its output, trimmed for Slack."""
    result = (runner or _runner.run)(argv(view, cfg.fleet_project), timeout=90)
    out = _ANSI.sub("", result.stdout or "")
    if result.returncode != 0:
        err = _ANSI.sub("", result.stderr or "").strip()
        out = f"{out}\n(fleet exited {result.returncode}: {err})".strip()
    return scrub(out)


def scrub(text: str, home: Path | None = None) -> str:
    """Shorten the home directory to ``~`` and cap the length."""
    home_str = str(home or Path.home())
    text = text.replace(home_str, "~").strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS].rstrip() + "\n… (truncated)"
    return text


def is_status_question(text: str, cfg: VegaConfig) -> bool:
    """True when the message is asking about fleet state rather than asking for work."""
    lowered = text.strip().casefold()
    return any(re.search(p, lowered) for p in cfg.status_patterns)
