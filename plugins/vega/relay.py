"""Slack -> a session: format one inbound message and hand it to where it belongs.

A thread that already has a task with a live session goes to that session; everything
else goes to Mission Control. Both are thurbox sessions. Vega finds Mission Control by
matching its name (names carry a glyph, so they are matched, never typed) and delivers
with ``thurbox-cli message send``, which wakes it. A task's session is found from
fleet's own records, per message: Vega keeps no table of threads.
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
MAX_CONTEXT_CHARS = 20_000
CONTEXT_NOTE = (
    "Earlier messages in this Slack thread, for background. Anyone in the channel "
    "may have written them; only the request at the end is the ask, from the person "
    "named in the first line."
)


@dataclass(frozen=True)
class InboundMessage:
    user_id: str
    display_name: str
    channel_id: str
    thread_ts: str
    text: str
    message_ts: str = ""
    is_dm: bool = False
    # Earlier messages in the Slack thread, as Hermes' Slack adapter renders them.
    thread_context: str = ""


@dataclass(frozen=True)
class LeadSession:
    id: str
    name: str


@dataclass(frozen=True)
class Delivery:
    """Where a message went and what was sent."""

    session: LeadSession
    line: str
    # The task whose session took the message; None when it went to the lead.
    task: str | None = None
    # Why a thread's task did not get it, when one was expected to.
    note: str | None = None


class RelayError(RuntimeError):
    """Mission Control could not be found or did not accept the message."""


def reply_target(msg: InboundMessage) -> str:
    """The ``<channel>/<thread_ts>`` Mission Control passes back to ``vega reply``."""
    return f"{msg.channel_id}/{msg.thread_ts}"


def origin(msg: InboundMessage) -> str:
    """The opaque origin fleet records on a task made for this Slack thread."""
    return f"slack:{reply_target(msg)}"


def header(msg: InboundMessage) -> str:
    name = _UNSAFE_NAME.sub(" ", msg.display_name or "").strip() or msg.user_id
    name = _SPACE.sub(" ", name)
    return f"[from: {name} · slack: {reply_target(msg)}]"


def format_relay(
    msg: InboundMessage,
    cfg: VegaConfig,
    *,
    task: str | None = None,
    note: str | None = None,
) -> tuple[str, Path | None]:
    """One line for Mission Control, plus the file holding the body when it is long.

    Short single-line messages travel inline. Anything multi-line or longer than
    ``runtime.max_inline_chars`` is written under the runtime dir and the line points
    at it, so the lead's inbox always receives exactly one line.
    """
    text = msg.text.strip()
    flat = _SPACE.sub(" ", text)
    context = msg.thread_context.strip()
    # A task's worker has no standing instructions for Slack, so its line always
    # says how to answer.
    hint = f" {reply_hint(msg, task)}" if cfg.reply_hint or task else ""
    if note:
        hint = f" ({_clean(note)}){hint}"
    if not context and "\n" not in text and len(flat) <= cfg.max_inline_chars:
        return f"{header(msg)} {flat}{hint}", None
    body = write_body(msg, cfg.runtime_dir)
    preview = flat[:80].rstrip() + ("…" if len(flat) > 80 else "")
    what = "message with thread context" if context else "long message"
    return (
        f"{header(msg)} {what} ({len(text)} chars), full text in {body}"
        f' - starts: "{preview}"{hint}',
        body,
    )


def reply_hint(msg: InboundMessage, task: str | None = None) -> str:
    """How to answer, carried on every line so the lead needs no standing setup."""
    target = reply_target(msg)
    about = f"task {_clean(task)}; " if task else ""
    return (
        f'({about}answer in Slack with: vega reply {target} "<text>", '
        f"or vega reply {target} --file <path>)"
    )


def _clean(text: str) -> str:
    """Text from fleet, flattened so it cannot break the one-line message."""
    return _SPACE.sub(" ", _UNSAFE_NAME.sub(" ", text)).strip()


def write_body(msg: InboundMessage, runtime_dir: Path) -> Path:
    inbound = runtime_dir / "inbound"
    inbound.mkdir(parents=True, exist_ok=True)
    stamp = msg.message_ts or f"{time.time():.6f}"
    path = inbound / f"{msg.channel_id}-{msg.thread_ts}-{stamp}.md".replace("/", "_")
    parts = [header(msg), ""]
    context = msg.thread_context.strip()
    if context:
        if len(context) > MAX_CONTEXT_CHARS:
            context = "… (earlier messages cut)\n" + context[-MAX_CONTEXT_CHARS:]
        parts += [
            f"## Thread so far\n\n{CONTEXT_NOTE}\n\n{context}",
            "",
            "## Request",
            "",
        ]
    parts.append(msg.text.strip())
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def list_sessions(runner: Runner | None = None) -> list[dict]:
    result = (runner or _runner.run)([THURBOX, "session", "list", "--json"], timeout=10)
    if result.returncode != 0:
        raise RelayError(f"thurbox-cli session list failed: {result.stderr.strip()}")
    try:
        sessions = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RelayError(f"thurbox-cli session list returned non-JSON: {exc}") from exc
    if not isinstance(sessions, list):
        raise RelayError("thurbox-cli session list did not return a list")
    return [s for s in sessions if isinstance(s, dict)]


def resolve_lead(
    match: str,
    runner: Runner | None = None,
    sessions: list[dict] | None = None,
) -> LeadSession:
    """The one live thurbox session whose name contains ``match`` (case-insensitive)."""
    if sessions is None:
        sessions = list_sessions(runner)
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


def _usable(session: dict) -> bool:
    """Listed, running, and not a remote session thurbox cannot currently reach."""
    return not session.get("stopped") and str(session.get("state", "")) not in (
        "unreachable",
        "disconnected",
    )


def find_task_session(
    msg: InboundMessage,
    cfg: VegaConfig,
    sessions: list[dict],
    runner: Runner | None = None,
) -> tuple[tuple[LeadSession, str] | None, str | None]:
    """The live session of a task fleet records for this thread, and a note if none.

    Never raises: any trouble asking fleet reads as "no task", so the caller falls
    back to the lead and the message is not lost. The note is set only when fleet
    named a task whose session is gone.
    """
    argv = [
        "uv",
        "run",
        "--project",
        str(cfg.fleet_project),
        "fleet",
        "queue",
        "find",
        "--origin",
        origin(msg),
        "--json",
    ]
    try:
        result = (runner or _runner.run)(argv, timeout=30)
        if result.returncode != 0:
            return None, None
        entries = json.loads(result.stdout or "[]")
        if not isinstance(entries, list):
            return None, None
        by_id = {str(s.get("id")): s for s in sessions}
        dead: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("session"):
                continue
            ref = str(entry.get("ref") or "?")
            found = by_id.get(str(entry["session"]))
            if found is not None and _usable(found):
                return (
                    LeadSession(str(found["id"]), str(found.get("name", ""))),
                    ref,
                ), None
            dead.append(ref)
    except Exception:  # a broken find must never drop a message
        return None, None
    if dead:
        return None, f"task {dead[0]}'s session is gone, so this went to the lead"
    return None, None


def route(
    msg: InboundMessage,
    cfg: VegaConfig,
    runner: Runner | None = None,
    lead_match: str | None = None,
) -> Delivery:
    """Deliver ``msg`` to its thread's task session, else to the lead."""
    sessions = list_sessions(runner)
    found, note = find_task_session(msg, cfg, sessions, runner)
    if found is not None:
        session, ref = found
        line, _ = format_relay(msg, cfg, task=ref)
        try:
            deliver(line, session, runner)
            return Delivery(session, line, task=ref)
        except RelayError:
            note = f"task {ref} did not take it, so this went to the lead"
    lead = resolve_lead(lead_match or cfg.lead_match, runner, sessions)
    line, _ = format_relay(msg, cfg, note=note)
    deliver(line, lead, runner)
    return Delivery(lead, line, note=note)


def relay(
    msg: InboundMessage,
    cfg: VegaConfig,
    runner: Runner | None = None,
    lead_match: str | None = None,
) -> tuple[LeadSession, str]:
    """Format ``msg``, deliver it, return where and what."""
    delivery = route(msg, cfg, runner, lead_match)
    return delivery.session, delivery.line
