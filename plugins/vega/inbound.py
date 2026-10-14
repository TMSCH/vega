"""One inbound Slack message, and the decision the gate makes about it.

Shared by the gateway hooks and the ``vega`` command: build the message from a Hermes
``SessionSource`` and decide whether its sender may talk to Vega. Everything admitted
reaches Vega's model, which runs the intake itself with the origin tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .access import decide
from .config import VegaConfig


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
class Outcome:
    kind: Literal["refused", "admitted"]
    reply: str | None
    detail: str


def from_source(source: Mapping[str, Any], text: str) -> InboundMessage:
    """Build a message from a Hermes ``SessionSource.to_dict()``.

    Hermes threads Slack replies: a top-level message's thread is its own ts, so the
    thread falls back to the message id when the source carries none.
    """
    message_ts = str(source.get("message_id") or "")
    return InboundMessage(
        user_id=str(source.get("user_id") or ""),
        display_name=str(source.get("user_name") or ""),
        channel_id=str(source.get("chat_id") or ""),
        thread_ts=str(source.get("thread_id") or message_ts),
        text=text,
        message_ts=message_ts,
        is_dm=source.get("chat_type") == "dm",
    )


def check(msg: InboundMessage, cfg: VegaConfig, *, is_bot: bool = False) -> Outcome:
    decision = decide(
        cfg,
        user_id=msg.user_id,
        channel_id=msg.channel_id,
        is_dm=msg.is_dm,
        is_bot=is_bot,
    )
    if not decision.allowed:
        return Outcome("refused", cfg.refusal, decision.reason)
    return Outcome("admitted", None, decision.reason)
