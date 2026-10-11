from __future__ import annotations

from pathlib import Path

import pytest
from vega import fleet
from vega.reply import ReplyError, Target, parse_target, post

from .conftest import CHANNEL, TS, FakeRunner


def test_parse_target():
    assert parse_target(f" {CHANNEL}/{TS} ") == Target(CHANNEL, TS)


@pytest.mark.parametrize(
    "raw", ["", CHANNEL, f"{CHANNEL}:{TS}", f"U0ABC/{TS}", f"{CHANNEL}/abc", "#ops/1.2"]
)
def test_parse_target_refuses_garbage(raw):
    with pytest.raises(ReplyError):
        parse_target(raw)


def test_dry_run_prints_and_runs_nothing():
    runner = FakeRunner()
    out = post(
        Target(CHANNEL, TS), "done ✅", profile="vega", dry_run=True, runner=runner
    )
    assert out == f"[dry-run] would post to slack:{CHANNEL}:{TS}:\ndone ✅"
    assert runner.calls == []


def test_post_goes_through_vegas_hermes_profile_on_stdin():
    runner = FakeRunner()
    out = post(
        Target(CHANNEL, TS), "hello", profile="vega", dry_run=False, runner=runner
    )
    assert out == f"posted to {CHANNEL}/{TS}"
    ((argv, stdin),) = runner.calls
    assert argv == [
        "hermes",
        "-p",
        "vega",
        "send",
        "--to",
        f"slack:{CHANNEL}:{TS}",
        "--quiet",
    ]
    assert stdin == "hello"


def test_post_refuses_empty_and_reports_failure():
    with pytest.raises(ReplyError, match="empty"):
        post(Target(CHANNEL, TS), "  ", profile="vega", dry_run=True)
    with pytest.raises(ReplyError, match="hermes send failed"):
        post(
            Target(CHANNEL, TS),
            "x",
            profile="vega",
            dry_run=False,
            runner=FakeRunner(hermes_rc=1),
        )


def test_fleet_views_are_fixed_read_only_commands(tmp_path: Path):
    for view in fleet.VIEWS:
        argv = fleet.argv(view, tmp_path)
        assert argv[:4] == ["uv", "run", "--project", str(tmp_path)]
        assert argv[4] == "fleet"
        assert not {"add", "dispatch", "block", "abandon", "learn", "topic"} & set(argv)
    with pytest.raises(ValueError):
        fleet.argv("queue add", tmp_path)


def test_read_view_scrubs_home_and_ansi(cfg):
    home = str(Path.home())
    runner = FakeRunner(fleet_out=f"\x1b[1mQUEUE\x1b[0m {home}/fleet/orchestration")
    assert fleet.read_view("status", cfg, runner) == "QUEUE ~/fleet/orchestration"


def test_scrub_truncates():
    out = fleet.scrub("x" * 5000, home=Path("/nowhere"))
    assert len(out) < 3600 and out.endswith("(truncated)")


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("what's running?", True),
        ("What is in flight right now", True),
        ("status?", True),
        ("queue status please", True),
        ("can you fix the login bug", False),
        ("add a status page to the app", False),
    ],
)
def test_status_questions(cfg, text, status):
    assert fleet.is_status_question(text, cfg) is status
