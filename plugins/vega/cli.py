"""The ``vega`` command: reply into Slack, simulate an inbound message, read fleet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, fleet, inbound, reply
from .relay import InboundMessage


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        cfg = config.load(args.config)
        return args.func(args, cfg)
    except (config.ConfigError, reply.ReplyError, ValueError) as exc:
        print(f"vega: {exc}", file=sys.stderr)
        return 2


def _reply(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    target = reply.parse_target(args.target)
    if args.file and args.text:
        raise reply.ReplyError("pass the reply as TEXT or --file, not both")
    if args.file == "-" or (not args.file and args.text is None):
        text = sys.stdin.read()
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = args.text
    dry_run = args.dry_run or cfg.dry_run
    print(reply.post(target, text, profile=cfg.hermes_profile, dry_run=dry_run))
    return 0


def _relay(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    """Run one fake Slack message through exactly the path the gateway uses."""
    msg = InboundMessage(
        user_id=args.user,
        display_name=args.name or args.user,
        channel_id=args.channel,
        thread_ts=args.thread_ts,
        text=args.text,
        message_ts=args.thread_ts,
        is_dm=args.dm,
    )
    outcome = inbound.handle(msg, cfg, lead_match=args.lead_match)
    print(f"outcome: {outcome.kind}")
    print(f"detail:  {outcome.detail}")
    if outcome.reply:
        print(f"slack reply: {outcome.reply}")
    if outcome.kind == "status":
        print("Vega's model would answer this from: vega status")
    return 1 if outcome.kind == "failed" else 0


def _status(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    print(fleet.read_view(args.view, cfg))
    return 0


def _config(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    where = cfg.source or f"{config.default_config_path()} (missing: defaults)"
    print(f"config:           {where}")
    print(f"allowed users:    {len(cfg.allowed_users)}")
    print(f"allowed channels: {len(cfg.allowed_channels)}")
    print(f"lead match:       {cfg.lead_match!r}")
    print(f"hermes profile:   {cfg.hermes_profile}")
    print(f"fleet project:    {cfg.fleet_project}")
    print(f"runtime dir:      {cfg.runtime_dir}")
    print(f"dry run:          {cfg.dry_run}")
    if not cfg.allowed_users:
        print("warning: the allowlist is empty, so everyone is refused")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vega", description=__doc__)
    p.add_argument("--version", action="version", version=f"vega {__version__}")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="vega.toml to read (default: $VEGA_CONFIG, else Vega's Hermes profile)",
    )
    sub = p.add_subparsers(required=True, metavar="COMMAND")

    r = sub.add_parser("reply", help="post into a Slack thread as Vega")
    r.add_argument("target", help="<channel id>/<thread_ts>, from the [from: …] prefix")
    r.add_argument("text", nargs="?", help="the reply; omit to read --file or stdin")
    r.add_argument("--file", help="read the reply from this file ('-' for stdin)")
    r.add_argument(
        "--dry-run", action="store_true", help="print instead of posting to Slack"
    )
    r.set_defaults(func=_reply)

    s = sub.add_parser(
        "relay",
        help="simulate one inbound Slack message (no Slack needed)",
        description=(
            "Feed a fake Slack message through Vega's allowlist, status check and "
            "relay. A relayed message really is sent with thurbox-cli message send, "
            "to the session --lead-match names (default: the config's lead_match)."
        ),
    )
    s.add_argument("text")
    s.add_argument("--user", required=True, help="fake sender's Slack user id")
    s.add_argument("--name", help="fake sender's display name")
    s.add_argument("--channel", required=True, help="fake channel id")
    s.add_argument("--thread-ts", required=True, help="fake thread ts")
    s.add_argument("--dm", action="store_true", help="treat it as a 1:1 DM")
    s.add_argument("--lead-match", help="deliver to the session matching this name")
    s.set_defaults(func=_relay)

    st = sub.add_parser("status", help="print one read-only fleet view")
    st.add_argument("view", nargs="?", default="status", choices=sorted(fleet.VIEWS))
    st.set_defaults(func=_status)

    c = sub.add_parser("config", help="show where the config is and what it says")
    c.set_defaults(func=_config)
    return p


if __name__ == "__main__":
    raise SystemExit(main())
