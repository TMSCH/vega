"""Vega's wiring into the Hermes gateway: two hooks and the origin tools.

* ``pre_gateway_dispatch`` runs before Hermes' own authorization. It refuses anyone off
  Vega's allowlist (one polite reply, then silence for a while) and drops the message,
  so no slash command, pairing or model turn ever runs for them. It fails closed. It
  also keeps the gateway's session store, which the heartbeat module needs to map the
  session key to the current session id.
* ``post_gateway_admission`` runs once per admitted message. It re-checks the allowlist,
  records the person's message verbatim in the thread's record (the topic prompt is
  written from it), and lets the model turn run. A heartbeat tick is recognised and not
  recorded as a person's message.
* The tools are the only ones Vega's Slack toolset carries (``config.yaml``). Each
  builds a fixed ``fleet`` argv in code; the origin is always this thread's.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from . import config, fleet, heartbeat, inbound, origin
from .access import decide
from .origin import Thread

logger = logging.getLogger(__name__)

TOOLSET = "vega"
REFUSAL_COOLDOWN_SECONDS = 600
# Hermes renders every heartbeat tick from HEARTBEAT_PROMPT_TEMPLATE (hermes_cli).
HEARTBEAT_MARK = "[Heartbeat — "
_last_refusal: dict[str, float] = {}
# Thread history the Slack adapter fetched, carried from pre_gateway_dispatch (which
# sees the event) to post_gateway_admission (which only gets the text).
_thread_context: dict[tuple[str, str], str] = {}
MAX_HELD_CONTEXTS = 200
# The gateway's session store, seen by pre_gateway_dispatch; None outside the gateway.
_session_store: Any = None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_gateway_admission", post_gateway_admission)
    for schema, handler in TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            description=schema["description"],
        )


# --- hooks -----------------------------------------------------------------------


async def pre_gateway_dispatch(
    event: Any = None, gateway: Any = None, session_store: Any = None, **_: Any
):
    global _session_store
    if session_store is not None:
        _session_store = session_store
    elif gateway is not None and getattr(gateway, "session_store", None) is not None:
        _session_store = gateway.session_store
    source = getattr(event, "source", None)
    if source is None or _platform(source) != "slack":
        return None
    try:
        cfg = config.load()
        msg = inbound.from_source(source.to_dict(), getattr(event, "text", "") or "")
        gate = inbound.check(msg, cfg, is_bot=bool(getattr(source, "is_bot", False)))
        if gate.kind != "refused":
            if not _is_heartbeat(event):
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
        msg = inbound.from_source(src, text or "")
        gate = inbound.check(msg, cfg)
        if gate.kind == "refused":
            return {"action": "handled", "reply": cfg.refusal}
        if (text or "").lstrip().startswith(HEARTBEAT_MARK):
            return None  # the gateway's tick for this thread, not a person's message
        origin.record_message(
            cfg,
            channel=msg.channel_id,
            thread_ts=msg.thread_ts,
            message_ts=msg.message_ts,
            user_id=msg.user_id,
            display_name=msg.display_name,
            text=msg.text,
            context=context,
        )
    except Exception:
        logger.exception("vega: could not record the message; the model still answers")
    return None


def _is_heartbeat(event: Any) -> bool:
    if getattr(event, "_heartbeat_session_id", None):
        return True
    return str(getattr(event, "text", "") or "").lstrip().startswith(HEARTBEAT_MARK)


def _addressed_to_vega(event: Any, msg: inbound.InboundMessage) -> bool:
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


async def _send(
    gateway: Any, source: Any, msg: inbound.InboundMessage, text: str
) -> None:
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


# --- the session a tool runs in -----------------------------------------------------


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return os.environ.get(name, "")
    return get_session_env(name, "")


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


def _thread(cfg: config.VegaConfig) -> Thread:
    """This conversation's Slack thread, which is its fleet origin."""
    if _session("HERMES_SESSION_PLATFORM") != "slack":
        raise origin.OriginError(
            "this conversation is not a Slack thread, so it is not a fleet origin"
        )
    channel = _session("HERMES_SESSION_CHAT_ID")
    thread_ts = _session("HERMES_SESSION_THREAD_ID") or _session(
        "HERMES_SESSION_MESSAGE_ID"
    )
    return origin.load_thread(cfg, channel, thread_ts)


def _asker() -> str:
    return origin.clean_name(_session("HERMES_SESSION_USER_NAME")) or _session(
        "HERMES_SESSION_USER_ID"
    )


def _heartbeat_session_id() -> str:
    return heartbeat.session_id_for(
        _session("HERMES_SESSION_KEY"), _session_store, _session("HERMES_SESSION_ID")
    )


def _arm_heartbeat(cfg: config.VegaConfig, thread: Thread) -> dict:
    """Arm this conversation's heartbeat unless it already is; never raises."""
    try:
        session_id = _heartbeat_session_id()
        armed = thread.heartbeat
        if armed.get("session_id") == session_id and heartbeat.is_active(session_id):
            return {"heartbeat": "active", "every_seconds": cfg.heartbeat_seconds}
        state = heartbeat.arm(session_id, cfg.heartbeat_seconds)
        thread.heartbeat = {"session_id": session_id, "armed_at": time.time()}
        origin.save_thread(cfg, thread)
        return {"heartbeat": "armed", "every_seconds": state["interval_seconds"]}
    except heartbeat.HeartbeatUnavailable as exc:
        logger.warning("vega: heartbeat not armed: %s", exc)
        return {"heartbeat": "unavailable", "detail": str(exc)}
    except Exception as exc:
        logger.exception("vega: heartbeat not armed")
        return {"heartbeat": "error", "detail": str(exc)}


def _clear_heartbeat(cfg: config.VegaConfig, thread: Thread) -> dict:
    session_id = thread.heartbeat.get("session_id") or _heartbeat_session_id()
    try:
        cleared = heartbeat.clear(session_id) if session_id else False
    except heartbeat.HeartbeatUnavailable:
        cleared = False
    thread.heartbeat = {}
    origin.save_thread(cfg, thread)
    return {"heartbeat": "cleared" if cleared else "none"}


# --- tools -----------------------------------------------------------------------


def _tool(fn: Callable[..., dict]) -> Callable[..., str]:
    """Load the config, re-check the allowlist, run, and answer as one JSON string."""

    def handler(args: dict | None = None, **_: Any) -> str:
        try:
            cfg = config.load()
            if not _session_allowed(cfg):
                return json.dumps(
                    {"error": "this conversation is not on Vega's allowlist"}
                )
            return json.dumps(fn(cfg, dict(args or {})), ensure_ascii=False)
        except (fleet.FleetError, origin.OriginError, ValueError) as exc:
            return json.dumps({"error": fleet.scrub(str(exc))})
        except Exception as exc:
            logger.exception("vega: tool failed")
            return json.dumps({"error": fleet.scrub(str(exc))})

    handler.__name__ = fn.__name__
    return handler


def _fleet_status(cfg: config.VegaConfig, args: dict) -> dict:
    view = str(args.get("view") or "status")
    if view not in fleet.VIEWS:
        raise fleet.FleetError(f"view must be one of {', '.join(fleet.VIEWS)}")
    return {"view": view, "output": fleet.read_view(view, cfg)}


def _show(cfg: config.VegaConfig, args: dict) -> dict:
    ref = fleet.check_ref(args.get("ref"))
    record = fleet.show(cfg, ref)
    return {"ref": ref, "output": fleet.scrub(record.get("raw", ""))}


def _find(cfg: config.VegaConfig, args: dict) -> dict:
    thread = _thread(cfg)
    live = origin.live_tasks(cfg, thread)
    return {
        "origin": thread.origin,
        "live": live,
        "known": {
            ref: rec.get("state", "") for ref, rec in sorted(thread.tasks.items())
        },
        "topics": thread.topics,
    }


def _topic_add(cfg: config.VegaConfig, args: dict) -> dict:
    thread = _thread(cfg)
    out = origin.topic_add(
        cfg, thread, slug=args.get("slug"), title=args.get("title"), by=_asker()
    )
    out["origin"] = thread.origin
    return out


def _task_add(cfg: config.VegaConfig, args: dict) -> dict:
    thread = _thread(cfg)
    out = origin.task_add(
        cfg,
        thread,
        topic=args.get("topic"),
        slug=args.get("slug"),
        title=args.get("title"),
        repo=args.get("repo"),
        branch=args.get("branch"),
        base=args.get("base"),
        publish=args.get("publish"),
        sections={key: args.get(key) for key, _ in origin.BRIEF_SECTIONS},
        by=_asker(),
    )
    out["origin"] = thread.origin
    out.update(_arm_heartbeat(cfg, thread))
    return out


def _dispatch(cfg: config.VegaConfig, args: dict) -> dict:
    thread = _thread(cfg)
    out = origin.dispatch(cfg, thread)
    if thread.open_refs():
        out.update(_arm_heartbeat(cfg, thread))
    return out


def _send_tool(cfg: config.VegaConfig, args: dict) -> dict:
    thread = _thread(cfg)
    return origin.send(cfg, thread, ref=args.get("ref"), answer=args.get("answer"))


def _news(cfg: config.VegaConfig, args: dict) -> dict:
    thread = _thread(cfg)
    out = origin.news(cfg, thread)
    out["lines"] = origin.render_news(out["news"])
    if out["done"]:
        out.update(_clear_heartbeat(cfg, thread))
        out["instruction"] = (
            "Every task of this thread is finished. Post the news above if any, "
            "otherwise reply exactly NO_REPLY."
        )
    else:
        out.update(_arm_heartbeat(cfg, thread))
        out["instruction"] = (
            "Post each line above in this thread, in plain words. "
            if out["news"]
            else "Nothing changed: reply exactly NO_REPLY."
        )
    return out


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


FLEET_STATUS_SCHEMA = {
    "name": "vega_fleet_status",
    "description": (
        "Read fleet's state with one read-only fleet command. view=status: the whole "
        "fleet (fuel, topics, tasks). view=fuel: fuel per account. view=queue: "
        "every topic and task re-checked against their pull requests. view=plan: what "
        "is ready, stalled or needs input. view=context: where each open topic stands. "
        "Never changes anything."
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

SHOW_SCHEMA = {
    "name": "vega_show",
    "description": "One task's whole fleet record (state, origin, artifact, progress).",
    "parameters": {
        "type": "object",
        "properties": {"ref": _str("The task ref, <topic>/<NN>-<task>.")},
        "required": ["ref"],
    },
}

FIND_SCHEMA = {
    "name": "vega_find",
    "description": (
        "The tasks this Slack thread owns: the live ones fleet lists for this thread's "
        "origin, plus every ref the thread opened and its last known state."
    ),
    "parameters": {"type": "object", "properties": {}},
}

TOPIC_ADD_SCHEMA = {
    "name": "vega_topic_add",
    "description": (
        "Open a fleet topic for this thread's request. The prompt is written by Vega "
        "from the person's messages verbatim and the requester is recorded; you supply "
        "only a slug and a title."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": _str("Short lowercase topic id, letters, digits and dashes."),
            "title": _str("One-line title for the topic."),
        },
        "required": ["slug", "title"],
    },
}

TASK_ADD_SCHEMA = {
    "name": "vega_task_add",
    "description": (
        "Add one task to a topic of this thread. Vega writes the brief file from the "
        "four sections, sets the origin to this thread, checks the repo against the "
        "operator's allowlist and records the requester. Workers share no context with "
        "you: each section must stand on its own."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": _str("The topic slug from vega_topic_add."),
            "slug": _str("Short lowercase task id."),
            "title": _str("One-line title; it becomes the worker's session name."),
            "repo": _str("Repository path, one of the allowlisted ones."),
            "branch": _str("The branch the worker commits on."),
            "base": _str("Optional base branch (default: the repository's main)."),
            "publish": {
                "type": "string",
                "enum": list(fleet.PUBLISH_METHODS),
                "description": "Optional: what the task must leave behind; default pr.",
            },
            "what_to_do": _str("Brief section: the goal and the work, from scratch."),
            "hard_constraints": _str("Brief section: what the worker must not do."),
            "coordination": _str("Brief section: other tasks or people it must mind."),
            "done_means": _str("Brief section: what finished looks like."),
        },
        "required": [
            "topic",
            "slug",
            "title",
            "repo",
            "branch",
            "what_to_do",
            "hard_constraints",
            "coordination",
            "done_means",
        ],
    },
}

DISPATCH_SCHEMA = {
    "name": "vega_dispatch",
    "description": (
        "Start this thread's ready tasks. Vega reads fleet's fuel first and dispatches "
        "only when every gated account is clearly above the reserve; otherwise the "
        "tasks stay queued for the conductor, and the result says so. Confirm with the "
        "person before this would start more than two workers."
    ),
    "parameters": {"type": "object", "properties": {}},
}

SEND_SCHEMA = {
    "name": "vega_send",
    "description": (
        "Answer one of this thread's live workers with one line, without interrupting "
        "it. Only refs vega_find lists as live for this thread are accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": _str("The task ref, <topic>/<NN>-<task>."),
            "answer": _str("One line, the answer to the worker's question."),
        },
        "required": ["ref", "answer"],
    },
}

NEWS_SCHEMA = {
    "name": "vega_fleet_news",
    "description": (
        "What changed for this thread's tasks since the thread was last told: started, "
        "shipped with the pull request link, landed, abandoned and why, a worker's "
        "question. Returns only news, with the lines to post; an empty list means "
        "nothing changed."
    ),
    "parameters": {"type": "object", "properties": {}},
}

tool_fleet_status = _tool(_fleet_status)
tool_show = _tool(_show)
tool_find = _tool(_find)
tool_topic_add = _tool(_topic_add)
tool_task_add = _tool(_task_add)
tool_dispatch = _tool(_dispatch)
tool_send = _tool(_send_tool)
tool_news = _tool(_news)

TOOLS: tuple[tuple[dict, Callable[..., str]], ...] = (
    (FLEET_STATUS_SCHEMA, tool_fleet_status),
    (SHOW_SCHEMA, tool_show),
    (FIND_SCHEMA, tool_find),
    (TOPIC_ADD_SCHEMA, tool_topic_add),
    (TASK_ADD_SCHEMA, tool_task_add),
    (DISPATCH_SCHEMA, tool_dispatch),
    (SEND_SCHEMA, tool_send),
    (NEWS_SCHEMA, tool_news),
)
