"""Mission Control -> Slack: post into a thread through Vega's own Hermes profile.

The Slack token lives only in Vega's profile (``.env`` in its ``HERMES_HOME``).
``hermes -p <profile> send`` reads it there and calls Slack directly, so fleet never
sees the token and no gateway needs to be running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import runner as _runner
from .runner import Runner

_CHANNEL = re.compile(r"^[CGD][A-Z0-9]{2,}$")
_TS = re.compile(r"^\d{6,}\.\d{1,6}$")


# Tokens Hermes would take from the environment over the profile's .env. A shell
# exporting another app's token must not make Vega post as that app.
FOREIGN_TOKEN_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_TOKEN")


class ReplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    channel_id: str
    thread_ts: str

    def hermes(self) -> str:
        return f"slack:{self.channel_id}:{self.thread_ts}"

    def __str__(self) -> str:
        return f"{self.channel_id}/{self.thread_ts}"


def parse_target(raw: str) -> Target:
    """``C0123ABCD/1712345678.123456`` -> Target; anything else is refused."""
    channel, sep, ts = raw.strip().partition("/")
    if not sep or not _CHANNEL.match(channel) or not _TS.match(ts):
        raise ReplyError(
            f"bad target {raw!r}: expected <channel id>/<thread_ts>, "
            "as given in the [from: … · slack: …] prefix"
        )
    return Target(channel, ts)


def post(
    target: Target,
    text: str,
    *,
    profile: str,
    dry_run: bool,
    runner: Runner | None = None,
) -> str:
    """Post ``text`` to the thread, or describe the post when ``dry_run``."""
    if not text.strip():
        raise ReplyError("nothing to send: the reply is empty")
    if dry_run:
        return f"[dry-run] would post to {target.hermes()}:\n{text.rstrip()}"
    argv = ["hermes", "-p", profile, "send", "--to", target.hermes(), "--quiet"]
    result = (runner or _runner.run)(
        argv, input=text, timeout=60, drop_env=FOREIGN_TOKEN_ENV
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReplyError(f"hermes send failed ({result.returncode}): {detail}")
    return f"posted to {target}"
