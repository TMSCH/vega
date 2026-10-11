"""Who may talk to Vega: an allowlist of Slack user ids and channel ids."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ANY_CHANNEL, VegaConfig


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def decide(
    cfg: VegaConfig,
    *,
    user_id: str | None,
    channel_id: str | None,
    is_dm: bool,
    is_bot: bool = False,
) -> Decision:
    """Allow only an allowlisted human, in a DM or an allowlisted channel.

    A 1:1 DM has no channel to allowlist, so only the sender is checked there. Group DMs
    count as channels. ``channels = ["*"]`` accepts any channel Vega is in. An empty
    user allowlist refuses everyone.
    """
    if is_bot:
        return Decision(False, "bot sender")
    if not user_id or user_id not in cfg.allowed_users:
        return Decision(False, "user not on the allowlist")
    if is_dm:
        return Decision(True, "allowlisted user in a DM")
    if ANY_CHANNEL in cfg.allowed_channels and channel_id:
        return Decision(True, "allowlisted user in a channel Vega was invited to")
    if not channel_id or channel_id not in cfg.allowed_channels:
        return Decision(False, "channel not on the allowlist")
    return Decision(True, "allowlisted user in an allowlisted channel")
