from __future__ import annotations

from pathlib import Path

import pytest
from vega import fleet
from vega.fleet import FleetError, FuelAccount

from .conftest import FUEL_LOW, FUEL_OK, ORIGIN, FakeRunner, show_text

# --- the subprocess environment -----------------------------------------------------


def test_runner_drops_names_and_prefixes(monkeypatch):
    from vega.runner import child_env, run

    monkeypatch.setenv("SLACK_BOT_TOKEN", "not-a-real-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "not-a-real-token")
    monkeypatch.setenv("THURBOX_SESSION", "launcher")
    monkeypatch.setenv("KEEP_ME", "1")
    env = child_env(fleet.DROP_ENV)
    assert env is not None
    assert not any(k.startswith("SLACK_") for k in env)
    assert "THURBOX_SESSION" not in env and env["KEEP_ME"] == "1"
    assert child_env(()) is None
    out = run(["/usr/bin/env"], drop_env=fleet.DROP_ENV).stdout
    assert "SLACK_BOT_TOKEN=" not in out and "KEEP_ME=1" in out


def test_fleet_commands_run_with_the_minimal_env_and_a_timeout(cfg):
    runner = FakeRunner()
    fleet.read_view("fuel", cfg, runner)
    (argv,) = runner.fleet_calls()
    assert argv == ["status", "--fuel"]
    assert runner.dropped == fleet.DROP_ENV
    assert runner.calls[0][0][:4] == ["uv", "run", "--project", str(cfg.fleet_project)]


def test_views_are_fixed_read_only_commands(tmp_path: Path):
    for view in fleet.VIEWS:
        argv = fleet.argv(view, tmp_path)
        assert argv[:5] == ["uv", "run", "--project", str(tmp_path), "fleet"]
        assert not {"add", "dispatch", "block", "abandon", "send", "topic"} & set(argv)
    with pytest.raises(ValueError):
        fleet.argv("queue add", tmp_path)


def test_read_view_scrubs_home_and_ansi(cfg):
    home = str(Path.home())
    runner = FakeRunner(fleet_out=f"\x1b[1mQUEUE\x1b[0m {home}/fleet/orchestration")
    assert fleet.read_view("status", cfg, runner) == "QUEUE ~/fleet/orchestration"


def test_read_view_reports_a_failure(cfg):
    runner = FakeRunner(rc={"status": 3})
    assert "(fleet exited 3: status refused)" in fleet.read_view("status", cfg, runner)


def test_scrub_truncates():
    out = fleet.scrub("x" * 5000, home=Path("/nowhere"))
    assert len(out) < 3600 and out.endswith("(truncated)")


# --- what the model may put on an argv ----------------------------------------------


@pytest.mark.parametrize("value", ["fix-login", "a1", "x" * 61])
def test_slugs(value):
    assert fleet.check_slug(value) == value


@pytest.mark.parametrize("value", ["", "-rf", "Fix Login", "a/b", "x" * 62, None, "é"])
def test_bad_slugs(value):
    with pytest.raises(FleetError):
        fleet.check_slug(value)


@pytest.mark.parametrize("value", ["topic/01-task", "a-b/99-c-d"])
def test_refs(value):
    assert fleet.check_ref(value) == value


@pytest.mark.parametrize(
    "value", ["topic", "topic/task", "topic/1-task", "-x/01-y", ""]
)
def test_bad_refs(value):
    with pytest.raises(FleetError):
        fleet.check_ref(value)


@pytest.mark.parametrize("value", ["fix/login", "feat-1", "release/v1.2"])
def test_branches(value):
    assert fleet.check_branch(value) == value


@pytest.mark.parametrize("value", ["-b", "a..b", "x.lock", "a/", "a b", ""])
def test_bad_branches(value):
    with pytest.raises(FleetError):
        fleet.check_branch(value)


def test_titles_and_lines():
    assert fleet.check_title("  Fix   the\nlogin  ") == "Fix the login"
    assert fleet.check_line("yes,\nuse main", "answer") == "yes, use main"
    for bad in ("", "-", "--help", "x" * 121):
        with pytest.raises(FleetError):
            fleet.check_title(bad)
    with pytest.raises(FleetError):
        fleet.check_line("-n", "answer")


def test_repo_must_be_allowlisted(cfg, repo, tmp_path):
    assert fleet.check_repo(cfg, str(repo)) == str(repo)
    assert fleet.check_repo(cfg, repo.name) == str(repo)
    for bad in (str(tmp_path / "other"), "-", "", "../app"):
        with pytest.raises(FleetError):
            fleet.check_repo(cfg, bad)
    from dataclasses import replace

    with pytest.raises(FleetError, match="no repositories"):
        fleet.check_repo(replace(cfg, repos=()), str(repo))


@pytest.mark.parametrize(
    "value", ["-x", "--help", "a b", "a;b", "$(id)", "a/b", "x" * 65, "é", ".hidden"]
)
def test_bad_agent_names_are_refused_before_any_subprocess(value, thurbox_cli):
    runner = FakeRunner()
    with pytest.raises(FleetError, match="letters, digits"):
        fleet.check_agent(value, runner)
    assert runner.calls == []


def test_a_registered_agent_is_checked_with_thurbox_cli(thurbox_cli):
    runner = FakeRunner(agents={"claude-coder", "codex_ops.v2"})
    assert fleet.check_agent(" claude-coder ", runner) == "claude-coder"
    assert fleet.check_agent("codex_ops.v2", runner) == "codex_ops.v2"
    argv = runner.calls[0][0]
    assert argv == [thurbox_cli, "agent", "launch-args", "claude-coder", "--json"]
    assert runner.dropped == fleet.DROP_ENV


def test_an_unregistered_agent_is_refused_by_name(thurbox_cli):
    runner = FakeRunner()
    with pytest.raises(FleetError, match="'nope' is not registered in thurbox"):
        fleet.check_agent("nope", runner)


def test_no_agent_means_no_check(monkeypatch):
    monkeypatch.setattr("vega.fleet.shutil.which", lambda name: None)
    runner = FakeRunner()
    assert fleet.check_agent(None, runner) is None
    assert fleet.check_agent("", runner) is None
    assert runner.calls == []


def test_an_agent_without_thurbox_cli_is_refused_clearly(monkeypatch):
    monkeypatch.setattr("vega.fleet.shutil.which", lambda name: None)
    runner = FakeRunner()
    with pytest.raises(FleetError, match="thurbox-cli is not on PATH"):
        fleet.check_agent("claude-coder", runner)
    assert runner.calls == []


def test_publish_methods():
    assert fleet.check_publish(None) is None and fleet.check_publish("pr") == "pr"
    with pytest.raises(FleetError):
        fleet.check_publish("tweet")


# --- fuel ----------------------------------------------------------------------------


def test_parse_fuel_reads_every_account():
    accounts = fleet.parse_fuel(FUEL_OK)
    assert [a.provider for a in accounts] == ["claude", "codex", "copilot"]
    assert accounts[0] == FuelAccount("claude", 79, 20, "fresh", False)
    assert fleet.parse_fuel("") == []


def test_fuel_gate_first_account_by_default():
    ok, why = fleet.fuel_gate(fleet.parse_fuel(FUEL_OK), (), 10)
    assert ok and "claude 79%" in why
    ok, why = fleet.fuel_gate(fleet.parse_fuel(FUEL_LOW), (), 10)
    assert not ok and "claude 27%" in why and "floor 30%" in why


def test_fuel_gate_named_providers_all_must_pass():
    accounts = fleet.parse_fuel(FUEL_OK)
    assert fleet.fuel_gate(accounts, ("claude", "codex"), 10)[0]
    ok, why = fleet.fuel_gate(accounts, ("claude", "copilot"), 10)
    assert not ok and "copilot 0%" in why
    ok, why = fleet.fuel_gate(accounts, ("gemini",), 10)
    assert not ok and "gemini ?%" in why
    assert not fleet.fuel_gate([], (), 10)[0]


def test_fuel_gate_refuses_stale_or_unavailable():
    stale = fleet.parse_fuel(
        FUEL_OK.replace(
            "stale\t0\nstate\tfresh\nprovider\tclaude",
            "stale\t1\nstate\tfresh\nprovider\tclaude",
        )
    )
    assert not fleet.fuel_gate(stale, (), 10)[0]
    down = fleet.parse_fuel(
        "reserve\t20\nremaining\t\nstate\tunavailable\nprovider\tclaude\n"
    )
    assert not fleet.fuel_gate(down, (), 10)[0]


# --- records -------------------------------------------------------------------------


def test_parse_show_reads_keys_and_repeats():
    text = show_text(
        "t/01-a",
        state="done",
        outcome="shipped",
        artifact="https://example.test/pr/1",
        brief="/q/t/01-a/BRIEF.md",
    )
    text += "    artifact:    other  https://example.test/pr/2\n"
    rec = fleet.parse_show(text)
    assert (rec["ref"], rec["title"], rec["state"]) == (
        "t/01-a",
        "Fix the thing",
        "done",
    )
    assert rec["host"] == "" and rec["origin"] == ORIGIN
    assert fleet.artifact_urls(rec) == [
        "https://example.test/pr/1",
        "https://example.test/pr/2",
    ]
    assert fleet.progress_path(rec) == Path("/q/t/01-a/progress.jsonl")
    assert fleet.progress_path({}) is None


def test_find_and_show_run_the_fixed_commands(cfg):
    runner = FakeRunner(
        find_rows=[
            {"ref": "t/01-a", "state": "dispatched", "session": "s", "host": None},
            {"ref": "../x"},
        ],
        shows={"t/01-a": show_text("t/01-a")},
    )
    assert fleet.find_origin(cfg, ORIGIN, runner) == [
        {"ref": "t/01-a", "state": "dispatched", "session": "s", "host": None}
    ]
    assert fleet.show(cfg, "t/01-a", runner)["state"] == "queued"
    assert runner.fleet_calls() == [
        ["queue", "find", "--origin", ORIGIN, "--json"],
        ["queue", "show", "t/01-a"],
    ]
    with pytest.raises(FleetError, match="no such task"):
        fleet.show(cfg, "t/02-b", runner)
