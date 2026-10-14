"""The ``vega`` command: check the config, read fleet, inspect a thread's record."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, fleet, inbound, origin


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        cfg = config.load(args.config)
        return args.func(args, cfg)
    except (
        config.ConfigError,
        fleet.FleetError,
        origin.OriginError,
        ValueError,
    ) as exc:
        print(f"vega: {exc}", file=sys.stderr)
        return 2


def _admit(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    """Run one fake Slack message through the gate and the thread record."""
    msg = inbound.InboundMessage(
        user_id=args.user,
        display_name=args.name or args.user,
        channel_id=args.channel,
        thread_ts=args.thread_ts,
        text=args.text,
        message_ts=args.message_ts or args.thread_ts,
        is_dm=args.dm,
    )
    outcome = inbound.check(msg, cfg)
    print(f"outcome: {outcome.kind}")
    print(f"detail:  {outcome.detail}")
    if outcome.kind == "refused":
        print(f"slack reply: {outcome.reply}")
        return 1
    thread = origin.record_message(
        cfg,
        channel=msg.channel_id,
        thread_ts=msg.thread_ts,
        message_ts=msg.message_ts,
        user_id=msg.user_id,
        display_name=msg.display_name,
        text=msg.text,
    )
    print(f"origin:  {thread.origin}")
    print(f"record:  {origin.thread_path(cfg, thread.channel, thread.thread_ts)}")
    print("Vega's model would now answer, with the origin tools.")
    return 0


def _status(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    print(fleet.read_view(args.view, cfg))
    return 0


def _show(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    record = fleet.show(cfg, fleet.check_ref(args.ref))
    print(fleet.scrub(record["raw"]))
    return 0


def _threads(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    threads = origin.list_threads(cfg)
    if not threads:
        print("no thread records")
        return 0
    for t in threads:
        beat = "heartbeat armed" if t.heartbeat else "no heartbeat"
        print(
            f"{t.origin}: {len(t.messages)} message(s), topics {t.topics or '-'}, "
            f"open {t.open_refs() or '-'}, {beat}"
        )
    return 0


def _news(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    channel, ts = origin.parse_origin(args.origin)
    thread = origin.load_thread(cfg, channel, ts)
    out = origin.news(cfg, thread)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    lines = origin.render_news(out["news"])
    print("\n".join(lines) if lines else "no news")
    print(
        f"open: {', '.join(out['open']) or 'none'}" + (" (done)" if out["done"] else "")
    )
    return 0


def _config(args: argparse.Namespace, cfg: config.VegaConfig) -> int:
    where = cfg.source or f"{config.default_config_path()} (missing: defaults)"
    print(f"config:           {where}")
    print(f"allowed users:    {len(cfg.allowed_users)}")
    print(f"allowed channels: {len(cfg.allowed_channels)}")
    print(f"fleet project:    {cfg.fleet_project}")
    print(f"repos allowed:    {len(cfg.repos)}")
    print(
        f"fuel gate:        {cfg.fuel_providers or 'first account'} + {cfg.fuel_margin}"
    )
    print(f"heartbeat every:  {cfg.heartbeat_seconds}s")
    print(f"runtime dir:      {cfg.runtime_dir}")
    if not cfg.allowed_users:
        print("warning: the allowlist is empty, so everyone is refused")
    if not cfg.repos:
        print("warning: fleet.repos is empty, so no task can be added")
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

    a = sub.add_parser(
        "admit",
        help="run one fake Slack message through the gate and the thread record",
    )
    a.add_argument("text")
    a.add_argument("--user", required=True, help="fake sender's Slack user id")
    a.add_argument("--name", help="fake sender's display name")
    a.add_argument("--channel", required=True, help="fake channel id")
    a.add_argument("--thread-ts", required=True, help="fake thread ts")
    a.add_argument("--message-ts", help="fake message ts (default: the thread ts)")
    a.add_argument("--dm", action="store_true", help="treat it as a 1:1 DM")
    a.set_defaults(func=_admit)

    st = sub.add_parser("status", help="print one read-only fleet view")
    st.add_argument("view", nargs="?", default="status", choices=sorted(fleet.VIEWS))
    st.set_defaults(func=_status)

    sh = sub.add_parser("show", help="print one task's fleet record")
    sh.add_argument("ref")
    sh.set_defaults(func=_show)

    th = sub.add_parser("threads", help="list the Slack threads Vega has records for")
    th.set_defaults(func=_threads)

    n = sub.add_parser(
        "news", help="what changed for a thread's tasks; records that it was told"
    )
    n.add_argument(
        "origin", help="slack:<channel>/<thread_ts> or <channel>/<thread_ts>"
    )
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=_news)

    c = sub.add_parser("config", help="show where the config is and what it says")
    c.set_defaults(func=_config)
    return p


if __name__ == "__main__":
    raise SystemExit(main())
