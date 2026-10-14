"""Arming a Hermes session heartbeat from inside a plugin tool.

Hermes registers a heartbeat watch from two places: the ``/heartbeat`` slash command and
the gateway's restore scan. The scan is not only for restarts: the gateway's heartbeat
poller runs it every poll (``gateway/run_goals.py`` ``_start_heartbeat_poller`` ->
``run_heartbeat_restore.restore_heartbeat_watches``), so any session whose profile store
holds an active ``heartbeat:<session_id>`` row is picked up within seconds. Writing that
row through the public ``hermes_cli.heartbeat.HeartbeatManager`` is therefore enough to
start a heartbeat; no private registration call is needed.

The one private-ish dependency is resolving the session id. ``HERMES_SESSION_ID`` is
published when an agent is built and reads empty on a reused cached agent, while
``HERMES_SESSION_KEY`` is bound on every turn. So the id is read from the gateway's
session store by key (``SessionStore.peek_session_id``, the same accessor the poller
uses), falling back to the env var. This module is the only place that knows any of it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PROMPT = (
    "Fleet check for this Slack thread. Call vega_fleet_news once. For every item it "
    "returns, post one short plain line in this thread: shipped with the pull request "
    "link, landed, abandoned and why, or a worker's question. Answer a worker with "
    "vega_send only when the answer is already in this thread; otherwise ask the "
    "person. If it returns no news, reply exactly NO_REPLY."
)


class HeartbeatUnavailable(RuntimeError):
    """Not running inside Hermes, or Hermes has no heartbeat support."""


def _manager(session_id: str) -> Any:
    try:
        from hermes_cli.heartbeat import HeartbeatManager
    except ImportError as exc:
        raise HeartbeatUnavailable("hermes_cli.heartbeat is not importable") from exc
    if not session_id:
        raise HeartbeatUnavailable("no session id to key the heartbeat on")
    return HeartbeatManager(session_id=session_id)


def session_id_for(session_key: str, store: Any = None, fallback: str = "") -> str:
    """The session id the gateway currently routes ``session_key`` to."""
    peek = getattr(store, "peek_session_id", None)
    if session_key and callable(peek):
        try:
            found = peek(session_key)
        except Exception:
            logger.debug("vega: peek_session_id failed", exc_info=True)
            found = None
        if found:
            return str(found)
    return fallback or ""


def arm(session_id: str, interval_seconds: int, prompt: str = PROMPT) -> dict:
    """Set (or replace) the session's heartbeat; the poller finds it next scan."""
    mgr = _manager(session_id)
    state = mgr.set(prompt, int(interval_seconds))
    return {
        "session_id": session_id,
        "interval_seconds": int(state.interval_seconds),
        "status": str(state.status),
    }


def is_active(session_id: str) -> bool:
    try:
        return bool(_manager(session_id).is_active())
    except HeartbeatUnavailable:
        return False


def clear(session_id: str) -> bool:
    return bool(_manager(session_id).clear())


def status_line(session_id: str) -> str:
    return str(_manager(session_id).status_line())
