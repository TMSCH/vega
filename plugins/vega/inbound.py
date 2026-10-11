"""The decision Vega makes for every Slack message, shared by the gateway and the CLI.

refuse it (not allowlisted), leave it to Vega's model (a status question), or relay it
to Mission Control and acknowledge. The gateway hooks and ``vega relay`` both call
:func:`handle`, so the dry run exercises exactly what Slack traffic does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .access import decide
from .config import VegaConfig
from .fleet import is_status_question
from .relay import InboundMessage, RelayError, relay
from .runner import Runner

RELAY_FAILED = (
    "I couldn't reach Mission Control just now, so nothing was passed on. "
    "Please try again in a few minutes."
)


@dataclass(frozen=True)
class Outcome:
    kind: Literal["refused", "status", "relayed", "failed"]
    reply: str | None
    detail: str


def from_source(
    source: Mapping[str, Any], text: str, thread_context: str = ""
) -> InboundMessage:
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
        thread_context=thread_context,
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
    return Outcome("relayed", None, decision.reason)


def handle(
    msg: InboundMessage,
    cfg: VegaConfig,
    *,
    is_bot: bool = False,
    runner: Runner | None = None,
    lead_match: str | None = None,
) -> Outcome:
    gate = check(msg, cfg, is_bot=is_bot)
    if gate.kind == "refused":
        return gate
    if is_status_question(msg.text, cfg):
        return Outcome("status", None, "left to Vega: read-only fleet status")
    try:
        lead, line = relay(msg, cfg, runner, lead_match=lead_match)
    except RelayError as exc:
        return Outcome("failed", RELAY_FAILED, str(exc))
    return Outcome("relayed", cfg.ack, f"sent to {lead.name!r} ({lead.id}): {line}")
