"""Vega's wiring into the Hermes gateway: two hooks and two tools.

* ``pre_gateway_dispatch`` runs before Hermes' own authorization. It refuses anyone off
  Vega's allowlist (one polite reply, then silence for a while) and drops the message,
  so no slash command, pairing or model turn ever runs for them. It fails closed.
* ``post_gateway_admission`` runs once per admitted message. It relays the message to
  Mission Control deterministically and acknowledges, without a model turn. Status
  questions are left to Vega's model, which answers them with ``vega_fleet_status``.
* ``vega_fleet_status`` and ``vega_relay_to_mission_control`` are the only tools Vega's
  Slack toolset carries (see ``config.yaml``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from . import config, fleet, inbound
from .access import decide
from .relay import InboundMessage, RelayError, relay

logger = logging.getLogger(__name__)

TOOLSET = "vega"
REFUSAL_COOLDOWN_SECONDS = 600
_last_refusal: dict[str, float] = {}
_pending: dict[str, InboundMessage] = {}
# Thread history the Slack adapter fetched, carried from pre_gateway_dispatch (which
# sees the event) to post_gateway_admission (which only gets the text).
_thread_context: dict[tuple[str, str], str] = {}
MAX_HELD_CONTEXTS = 200


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_gateway_admission", post_gateway_admission)
    ctx.register_tool(
        name="vega_fleet_status",
        toolset=TOOLSET,
        schema=FLEET_STATUS_SCHEMA,
        handler=tool_fleet_status,
        description=FLEET_STATUS_SCHEMA["description"],
    )
    ctx.register_tool(
        name="vega_relay_to_mission_control",
        toolset=TOOLSET,
        schema=RELAY_SCHEMA,
        handler=tool_relay,
        description=RELAY_SCHEMA["description"],
    )


# --- hooks -----------------------------------------------------------------------


async def pre_gateway_dispatch(event: Any = None, gateway: Any = None, **_: Any):
    source = getattr(event, "source", None)
    if source is None or _platform(source) != "slack":
        return None
    try:
        cfg = config.load()
        msg = inbound.from_source(source.to_dict(), getattr(event, "text", "") or "")
        gate = inbound.check(msg, cfg, is_bot=bool(getattr(source, "is_bot", False)))
        if gate.kind != "refused":
            _hold_context(msg.channel_id, msg.message_ts, event)
            return None
        logger.info(
            "vega: refusing %s in %s (%s)", msg.user_id, msg.channel_id, gate.detail
        )
        if (
            _addressed_to_vega(event, msg)
            and not getattr(source, "is_bot", False)
            and _refusal_due(msg.user_id)
        ):
            await _send(gateway, source, msg, cfg.refusal)
        return {"action": "skip", "reason": f"vega: {gate.detail}"}
    except Exception:
        # Fail closed: a broken config or check must not let a stranger through.
        logger.exception("vega: access check failed; dropping the message")
        return {"action": "skip", "reason": "vega: access check failed"}


async def post_gateway_admission(
    session_key: str = "",
    platform: str = "",
    source: dict | None = None,
    text: str = "",
    **_: Any,
):
    if platform != "slack":
        return None
    try:
        cfg = config.load()
        src = source or {}
        context = _thread_context.pop(
            (str(src.get("chat_id") or ""), str(src.get("message_id") or "")), ""
        )
        msg = inbound.from_source(src, text or "", thread_context=context)
        outcome = await asyncio.to_thread(inbound.handle, msg, cfg)
    except Exception:
        logger.exception("vega: relay failed")
        return {"action": "handled", "reply": inbound.RELAY_FAILED}
    if outcome.kind == "status":
        _pending[session_key] = msg
        return None
    logger.info("vega: %s - %s", outcome.kind, outcome.detail)
    return {"action": "handled", "reply": outcome.reply}


def _addressed_to_vega(event: Any, msg: InboundMessage) -> bool:
    """A DM or an @Vega. A plain reply in a thread Vega is part of reaches the
    gateway too (``reply_expected`` is unset), but is chatter among teammates, and
    someone off the allowlist is ignored there rather than told off."""
    return msg.is_dm or getattr(event, "reply_expected", None) is True


def _hold_context(channel_id: str, message_ts: str, event: Any) -> None:
    context = getattr(event, "channel_context", None)
    if not context or not message_ts:
        return
    if len(_thread_context) >= MAX_HELD_CONTEXTS:
        _thread_context.pop(next(iter(_thread_context)))
    _thread_context[(channel_id, message_ts)] = str(context)


def _platform(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def _refusal_due(user_id: str) -> bool:
    now = time.monotonic()
    last = _last_refusal.get(user_id)
    if last is not None and now - last < REFUSAL_COOLDOWN_SECONDS:
        return False
    _last_refusal[user_id] = now
    return True


async def _send(gateway: Any, source: Any, msg: InboundMessage, text: str) -> None:
    """Best effort: reply through the adapter that received the message."""
    try:
        adapter = None
        lookup = getattr(gateway, "_authorization_adapter", None)
        if callable(lookup):
            adapter = lookup(source.platform, getattr(source, "profile", None))
        if adapter is None:
            adapter = (getattr(gateway, "adapters", None) or {}).get(source.platform)
        if adapter is None:
            logger.warning("vega: no Slack adapter to send the refusal through")
            return
        await adapter.send(msg.channel_id, text, metadata={"thread_id": msg.thread_ts})
    except Exception:
        logger.exception("vega: could not send the refusal")


# --- tools -----------------------------------------------------------------------

FLEET_STATUS_SCHEMA = {
    "name": "vega_fleet_status",
    "description": (
        "Read fleet's current state with one read-only fleet command. "
        "view=status: the whole fleet in one reading (provider fuel, queue, topics "
        "and their tasks). view=queue: every topic and task, re-checked against "
        "their pull requests. view=context: where each open topic stands and what is "
        "ready or stalled. Never changes anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "enum": sorted(fleet.VIEWS),
                "description": "Which read-only view to run.",
            }
        },
        "required": ["view"],
    },
}

RELAY_SCHEMA = {
    "name": "vega_relay_to_mission_control",
    "description": (
        "Hand the current Slack message to Mission Control, which decides and "
        "dispatches work and answers in this thread. Use it whenever the message asks "
        "for anything other than a read of fleet's status. Pass the person's message "
        "verbatim."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The person's message, verbatim, not paraphrased.",
            }
        },
        "required": ["message"],
    },
}


def tool_fleet_status(args: dict, **_: Any) -> str:
    try:
        cfg = config.load()
        if not _session_allowed(cfg):
            return json.dumps({"error": "this conversation is not on Vega's allowlist"})
        view = str(args.get("view") or "status")
        return json.dumps({"view": view, "output": fleet.read_view(view, cfg)})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def tool_relay(args: dict, **_: Any) -> str:
    try:
        cfg = config.load()
        if not _session_allowed(cfg):
            return json.dumps({"error": "this conversation is not on Vega's allowlist"})
        msg = _pending.pop(
            _session("HERMES_SESSION_KEY"), None
        ) or _message_from_session(str(args.get("message") or ""))
        if msg is None or not msg.text.strip():
            return json.dumps({"error": "no Slack message to relay in this session"})
        lead, line = relay(msg, cfg)
        return json.dumps({"relayed_to": lead.name, "line": line, "ack": cfg.ack})
    except RelayError as exc:
        return json.dumps({"error": f"Mission Control unreachable: {exc}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return os.environ.get(name, "")
    return get_session_env(name, "")


def _message_from_session(text: str) -> InboundMessage | None:
    if _session("HERMES_SESSION_PLATFORM") != "slack":
        return None
    message_ts = _session("HERMES_SESSION_MESSAGE_ID")
    return InboundMessage(
        user_id=_session("HERMES_SESSION_USER_ID"),
        display_name=_session("HERMES_SESSION_USER_NAME"),
        channel_id=_session("HERMES_SESSION_CHAT_ID"),
        thread_ts=_session("HERMES_SESSION_THREAD_ID") or message_ts,
        text=text,
        message_ts=message_ts,
        is_dm=_session("HERMES_SESSION_CHAT_TYPE") == "dm",
    )


def _session_allowed(cfg: config.VegaConfig) -> bool:
    """The operator at a local Hermes prompt is trusted; on Slack, re-check; else no."""
    platform = _session("HERMES_SESSION_PLATFORM")
    if platform in {"", "cli"}:
        return True
    if platform != "slack":
        return False
    return decide(
        cfg,
        user_id=_session("HERMES_SESSION_USER_ID"),
        channel_id=_session("HERMES_SESSION_CHAT_ID"),
        is_dm=_session("HERMES_SESSION_CHAT_TYPE") == "dm",
    ).allowed
