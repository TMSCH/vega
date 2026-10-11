"""Slack -> Mission Control: format one inbound message and hand it to the lead session.

Mission Control is a thurbox session. Vega finds it by matching its name (names carry a
glyph, so they are matched, never typed) and delivers with ``thurbox-cli message send``,
which wakes it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import runner as _runner
from .config import VegaConfig
from .runner import Runner

THURBOX = "thurbox-cli"
MESSAGE_KIND = "slack"
# thurbox stamps the sender from these. Vega is not the session that launched it,
# so a gateway started from inside a thurbox session must not claim to be it.
LAUNCHER_ENV = ("THURBOX_SESSION", "THURBOX_TASK")
_UNSAFE_NAME = re.compile(r"[\[\]\r\n\t·]+")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class InboundMessage:
    user_id: str
    display_name: str
    channel_id: str
    thread_ts: str
    text: str
    message_ts: str = ""
    is_dm: bool = False


@dataclass(frozen=True)
class LeadSession:
    id: str
    name: str


class RelayError(RuntimeError):
    """Mission Control could not be found or did not accept the message."""


def reply_target(msg: InboundMessage) -> str:
    """The ``<channel>/<thread_ts>`` Mission Control passes back to ``vega reply``."""
    return f"{msg.channel_id}/{msg.thread_ts}"


def header(msg: InboundMessage) -> str:
    name = _UNSAFE_NAME.sub(" ", msg.display_name or "").strip() or msg.user_id
    name = _SPACE.sub(" ", name)
    return f"[from: {name} · slack: {reply_target(msg)}]"


def format_relay(msg: InboundMessage, cfg: VegaConfig) -> tuple[str, Path | None]:
    """One line for Mission Control, plus the file holding the body when it is long.

    Short single-line messages travel inline. Anything multi-line or longer than
    ``runtime.max_inline_chars`` is written under the runtime dir and the line points
    at it, so the lead's inbox always receives exactly one line.
    """
    text = msg.text.strip()
    flat = _SPACE.sub(" ", text)
    if "\n" not in text and len(flat) <= cfg.max_inline_chars:
        return f"{header(msg)} {flat}", None
    body = write_body(msg, cfg.runtime_dir)
    preview = flat[:80].rstrip() + ("…" if len(flat) > 80 else "")
    return (
        f"{header(msg)} long message ({len(text)} chars), full text in {body}"
        f' - starts: "{preview}"',
        body,
    )


def write_body(msg: InboundMessage, runtime_dir: Path) -> Path:
    inbound = runtime_dir / "inbound"
    inbound.mkdir(parents=True, exist_ok=True)
    stamp = msg.message_ts or f"{time.time():.6f}"
    path = inbound / f"{msg.channel_id}-{msg.thread_ts}-{stamp}.md".replace("/", "_")
    path.write_text(
        f"{header(msg)}\n\n{msg.text.strip()}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def resolve_lead(match: str, runner: Runner | None = None) -> LeadSession:
    """The one live thurbox session whose name contains ``match`` (case-insensitive)."""
    result = (runner or _runner.run)([THURBOX, "session", "list", "--json"], timeout=10)
    if result.returncode != 0:
        raise RelayError(f"thurbox-cli session list failed: {result.stderr.strip()}")
    try:
        sessions = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RelayError(f"thurbox-cli session list returned non-JSON: {exc}") from exc
    needle = match.casefold().strip()
    found = [
        LeadSession(s["id"], s.get("name", ""))
        for s in sessions
        if needle
        and needle in str(s.get("name", "")).casefold()
        and not s.get("stopped")
    ]
    if len(found) > 1:
        exact = [s for s in found if _bare(s.name) == needle]
        found = exact if len(exact) == 1 else found
    if not found:
        raise RelayError(f"no live thurbox session matches {match!r}")
    if len(found) > 1:
        names = ", ".join(repr(s.name) for s in found)
        raise RelayError(f"{len(found)} sessions match {match!r}: {names}")
    return found[0]


def _bare(name: str) -> str:
    """A session name without its leading glyph, for breaking ties."""
    return re.sub(r"^[^\w]+", "", name).strip().casefold()


def deliver(line: str, lead: LeadSession, runner: Runner | None = None) -> None:
    argv = [
        THURBOX,
        "message",
        "send",
        "--to",
        lead.id,
        "--kind",
        MESSAGE_KIND,
        "--body",
        line,
    ]
    result = (runner or _runner.run)(argv, timeout=15, drop_env=LAUNCHER_ENV)
    if result.returncode != 0:
        raise RelayError(
            f"thurbox-cli message send failed: {result.stderr.strip() or result.stdout}"
        )


def relay(
    msg: InboundMessage,
    cfg: VegaConfig,
    runner: Runner | None = None,
    lead_match: str | None = None,
) -> tuple[LeadSession, str]:
    """Format ``msg``, deliver it to Mission Control, return where and what."""
    lead = resolve_lead(lead_match or cfg.lead_match, runner)
    line, _ = format_relay(msg, cfg)
    deliver(line, lead, runner)
    return lead, line
