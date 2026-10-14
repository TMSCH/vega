from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from vega import origin
from vega.fleet import FleetError
from vega.origin import OriginError, Thread

from .conftest import CHANNEL, FUEL_LOW, ORIGIN, TS, USER, FakeRunner, show_text

SECTIONS = {
    "what_to_do": "Add a retry to the nightly export.",
    "hard_constraints": "No schema changes.",
    "coordination": "Nothing else touches the exporter.",
    "done_means": "A PR with a test for the retry.",
}


def record(cfg, text="please add a retry to the nightly export", ts=TS, name="Ada"):
    return origin.record_message(
        cfg,
        channel=CHANNEL,
        thread_ts=TS,
        message_ts=ts,
        user_id=USER,
        display_name=name,
        text=text,
    )


# --- identity and the record -------------------------------------------------------


def test_origin_string_and_parse():
    assert origin.origin_of(CHANNEL, TS) == ORIGIN
    assert origin.parse_origin(ORIGIN) == (CHANNEL, TS)
    assert origin.parse_origin(f"{CHANNEL}/{TS}") == (CHANNEL, TS)
    for bad in [("general", TS), (CHANNEL, "now"), ("", "")]:
        with pytest.raises(OriginError):
            origin.origin_of(*bad)
    with pytest.raises(OriginError):
        origin.parse_origin("thurbox:abc")


def test_record_keeps_messages_verbatim_private_and_deduped(cfg):
    thread = record(cfg, "line one\n  line   two  ")
    record(cfg, "line one\n  line   two  ")  # the same message again
    thread = record(cfg, "and a follow-up", ts="1700000000.000200", name="Ada [x]\n")
    assert [m["text"] for m in thread.messages] == [
        "line one\n  line   two",
        "and a follow-up",
    ]
    assert thread.messages[1]["name"] == "Ada x"
    path = origin.thread_path(cfg, CHANNEL, TS)
    assert path.stat().st_mode & 0o777 == 0o600
    assert origin.load_thread(cfg, CHANNEL, TS) == thread
    assert [t.origin for t in origin.list_threads(cfg)] == [ORIGIN]


def test_record_keeps_thread_context_once_and_capped(cfg):
    big = "A: " + "x" * (origin.MAX_CONTEXT_CHARS + 50)
    thread = origin.record_message(
        cfg,
        channel=CHANNEL,
        thread_ts=TS,
        message_ts=TS,
        user_id=USER,
        display_name="Ada",
        text="hi",
        context=big,
    )
    assert thread.context.startswith("… (earlier messages cut)")
    thread = origin.record_message(
        cfg,
        channel=CHANNEL,
        thread_ts=TS,
        message_ts="2.1",
        user_id=USER,
        display_name="Ada",
        text="again",
        context="short",
    )
    assert thread.context.startswith("… (earlier")


# --- intake ---------------------------------------------------------------------------


def test_topic_add_writes_the_prompt_verbatim_and_records_the_asker(cfg, fake):
    thread = record(cfg)
    record(cfg, "and please keep the old job name", ts="1700000000.000200")
    thread = origin.load_thread(cfg, CHANNEL, TS)
    out = origin.topic_add(
        cfg,
        thread,
        slug="nightly-retry",
        title="Nightly retry",
        by="Ada L",
        runner=fake,
    )
    assert out["topic"] == "nightly-retry"
    prompt = Path(out["prompt_file"])
    body = prompt.read_text()
    assert "please add a retry to the nightly export" in body
    assert "and please keep the old job name" in body
    assert ORIGIN in body and prompt.stat().st_mode & 0o777 == 0o600
    (argv,) = fake.fleet_calls()
    assert argv == [
        "queue",
        "topic",
        "add",
        "nightly-retry",
        "--title",
        "Nightly retry",
        "--prompt-file",
        str(prompt),
        "--by",
        "Ada L",
    ]
    assert origin.load_thread(cfg, CHANNEL, TS).topics == ["nightly-retry"]


def test_topic_add_needs_a_message_and_valid_values(cfg, fake):
    thread = Thread(CHANNEL, TS)
    with pytest.raises(OriginError, match="no message"):
        origin.topic_add(cfg, thread, slug="x", title="T", by="A", runner=fake)
    thread = record(cfg)
    with pytest.raises(FleetError):
        origin.topic_add(cfg, thread, slug="Bad Slug", title="T", by="A", runner=fake)
    with pytest.raises(FleetError):
        origin.topic_add(cfg, thread, slug="ok", title="--evil", by="A", runner=fake)
    assert fake.fleet_calls() == []


def test_task_add_sets_the_origin_in_code_and_no_reply(cfg, fake, repo):
    thread = record(cfg)
    out = origin.task_add(
        cfg,
        thread,
        topic="nightly-retry",
        slug="add-retry",
        title="Add the retry",
        repo=str(repo),
        branch="fix/retry",
        sections=SECTIONS,
        by="Ada",
        publish="pr",
        base="main",
        runner=fake,
    )
    assert out["ref"] == "nightly-retry/01-add-retry"
    (argv,) = fake.fleet_calls()
    assert argv[:4] == ["queue", "add", "nightly-retry", "add-retry"]
    assert argv[argv.index("--origin") + 1] == ORIGIN
    assert "--reply" not in argv
    assert argv[argv.index("--repo") + 1] == str(repo)
    assert argv[argv.index("--base") + 1] == "main"
    brief = Path(argv[argv.index("--brief-file") + 1])
    text = brief.read_text()
    for heading in (
        "## What to do",
        "## Hard constraints",
        "## Coordination",
        "## Done means",
    ):
        assert heading in text
    assert brief.stat().st_mode & 0o777 == 0o600
    assert origin.load_thread(cfg, CHANNEL, TS).tasks[out["ref"]]["state"] == "queued"


def test_task_add_refuses_bad_input_before_running_anything(cfg, fake, repo, tmp_path):
    thread = record(cfg)
    good = dict(
        topic="t",
        slug="s",
        title="T",
        repo=str(repo),
        branch="b",
        sections=SECTIONS,
        by="A",
    )
    for bad in (
        {"repo": str(tmp_path / "elsewhere")},
        {"branch": "-x"},
        {"sections": {**SECTIONS, "done_means": ""}},
        {"sections": {**SECTIONS, "what_to_do": "x" * cfg.max_brief_chars}},
        {"publish": "tweet"},
        {"base": "a..b"},
    ):
        with pytest.raises((FleetError, OriginError)):
            origin.task_add(cfg, thread, **{**good, **bad}, runner=fake)
    assert fake.fleet_calls() == []


def test_task_add_passes_a_registered_agent(cfg, fake, repo, thurbox_cli):
    thread = record(cfg)
    origin.task_add(
        cfg,
        thread,
        topic="t",
        slug="s",
        title="T",
        repo=str(repo),
        branch="b",
        sections=SECTIONS,
        by="A",
        agent="claude-coder",
        runner=fake,
    )
    assert fake.thurbox_calls() == [["agent", "launch-args", "claude-coder", "--json"]]
    (argv,) = fake.fleet_calls()
    assert argv[argv.index("--agent") + 1] == "claude-coder"


def test_task_add_without_an_agent_passes_no_flag(cfg, fake, repo):
    thread = record(cfg)
    origin.task_add(
        cfg,
        thread,
        topic="t",
        slug="s",
        title="T",
        repo=str(repo),
        branch="b",
        sections=SECTIONS,
        by="A",
        runner=fake,
    )
    (argv,) = fake.fleet_calls()
    assert "--agent" not in argv
    assert fake.thurbox_calls() == []


def test_task_add_refuses_an_unusable_agent_before_queueing(
    cfg, fake, repo, thurbox_cli, monkeypatch
):
    thread = record(cfg)
    good = dict(
        topic="t",
        slug="s",
        title="T",
        repo=str(repo),
        branch="b",
        sections=SECTIONS,
        by="A",
    )
    with pytest.raises(FleetError, match="letters, digits"):
        origin.task_add(cfg, thread, **good, agent="--agent", runner=fake)
    with pytest.raises(FleetError, match="'ghost' is not registered in thurbox"):
        origin.task_add(cfg, thread, **good, agent="ghost", runner=fake)
    monkeypatch.setattr("vega.fleet.shutil.which", lambda name: None)
    with pytest.raises(FleetError, match="thurbox-cli is not on PATH"):
        origin.task_add(cfg, thread, **good, agent="claude-coder", runner=fake)
    assert fake.fleet_calls() == []
    assert not (cfg.runtime_dir / "briefs").exists()


def test_task_add_reports_fleet_refusal(cfg, fake, repo):
    fake.rc["queue add"] = 2
    thread = record(cfg)
    with pytest.raises(FleetError, match="queue add refused"):
        origin.task_add(
            cfg,
            thread,
            topic="t",
            slug="s",
            title="T",
            repo=str(repo),
            branch="b",
            sections=SECTIONS,
            by="A",
            runner=fake,
        )


# --- dispatch --------------------------------------------------------------------


def test_dispatch_reads_fuel_then_dispatches_only_this_origin(cfg, fake):
    thread = Thread(CHANNEL, TS)
    out = origin.dispatch(cfg, thread, fake)
    assert out["dispatched"] is True and "claude 79%" in out["reason"]
    assert fake.fleet_calls() == [
        ["status", "--fuel"],
        ["queue", "dispatch", "--origin", ORIGIN],
    ]


def test_dispatch_holds_below_the_margin_or_when_fuel_is_unreadable(cfg):
    low = FakeRunner(fuel_out=FUEL_LOW)
    out = origin.dispatch(cfg, Thread(CHANNEL, TS), low)
    assert out["dispatched"] is False and "conductor" in out["left_for"]
    assert low.fleet_calls() == [["status", "--fuel"]]
    down = FakeRunner(rc={"status": 1})
    assert origin.dispatch(cfg, Thread(CHANNEL, TS), down)["dispatched"] is False


# --- talking to a worker ---------------------------------------------------------


def test_send_only_to_this_threads_live_tasks_and_rate_capped(cfg, fake):
    thread = record(cfg)
    fake.find_rows = [
        {"ref": "t/01-a", "state": "dispatched", "session": "s", "host": None}
    ]
    out = origin.send(cfg, thread, ref="t/01-a", answer="use   main", runner=fake)
    assert out["sent"] == "use main"
    assert fake.fleet_calls()[-1] == ["queue", "send", "t/01-a", "use main"]
    with pytest.raises(OriginError, match="not a live task"):
        origin.send(cfg, thread, ref="t/02-b", answer="x", runner=fake)
    origin.send(cfg, thread, ref="t/01-a", answer="second", runner=fake)
    with pytest.raises(OriginError, match="this hour"):
        origin.send(cfg, thread, ref="t/01-a", answer="third", runner=fake)
    with pytest.raises(FleetError):
        origin.send(cfg, thread, ref="t/01-a", answer="-n", runner=fake)


# --- news --------------------------------------------------------------------------


def progress(tmp_path: Path, ref: str, rows: list[dict]) -> str:
    folder = tmp_path / "queue" / ref
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "progress.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(folder / "BRIEF.md")


def test_news_reports_each_change_once(cfg, fake, tmp_path):
    ref = "t/01-a"
    thread = Thread(
        CHANNEL,
        TS,
        tasks={
            ref: {"state": "queued", "outcome": "", "artifacts": [], "needs_input": ""}
        },
    )
    brief = progress(tmp_path, ref, [])
    fake.shows[ref] = show_text(ref, state="queued", brief=brief)
    assert origin.news(cfg, thread, fake)["news"] == []

    fake.shows[ref] = show_text(ref, state="dispatched", brief=brief)
    out = origin.news(cfg, thread, fake)
    assert [e["kind"] for e in out["news"]] == ["started"] and not out["done"]
    assert origin.news(cfg, thread, fake)["news"] == []

    progress(
        tmp_path,
        ref,
        [
            {
                "declare": {"state": "needs-input", "why": "which base?"},
                "observed": "2026-01-01T00:00:01+00:00",
            }
        ],
    )
    out = origin.news(cfg, thread, fake)
    assert out["news"] == [
        {
            "ref": ref,
            "kind": "needs_input",
            "title": "Fix the thing",
            "question": "which base?",
            "observed": "2026-01-01T00:00:01+00:00",
        }
    ]
    assert origin.news(cfg, thread, fake)["news"] == []

    fake.shows[ref] = show_text(
        ref,
        state="done",
        outcome="shipped",
        artifact="https://example.test/pr/7",
        brief=brief,
    )
    out = origin.news(cfg, thread, fake)
    assert [e["kind"] for e in out["news"]] == ["shipped"]
    assert out["news"][0]["urls"] == ["https://example.test/pr/7"]
    assert "https://example.test/pr/7" in origin.render_news(out["news"])[0]

    fake.shows[ref] = show_text(
        ref,
        state="landed",
        outcome="shipped",
        artifact="https://example.test/pr/7",
        brief=brief,
    )
    out = origin.news(cfg, thread, fake)
    assert [e["kind"] for e in out["news"]] == ["landed"] and out["done"]
    assert origin.news(cfg, thread, fake) == {"news": [], "open": [], "done": True}
    # A finished task is not shown again.
    assert ["queue", "show", ref] not in fake.fleet_calls()[-1:]


def test_news_carries_an_answer_only_result_once(cfg, fake, tmp_path):
    """A task that ends without a pull request is 'finished' with its result body."""
    ref = "t/01-count"
    thread = Thread(CHANNEL, TS, tasks={ref: {"state": "dispatched"}})
    brief = progress(tmp_path, ref, [])
    result = Path(brief).parent / "result.md"
    result.write_text(
        "---\noutcome: not-applicable\nlearned:\n  - fact: secret\n---\n"
        "**4,140 users** signed up in the window.\nNo data modified.\n"
    )
    fake.shows[ref] = show_text(
        ref, state="done", outcome="not-applicable", brief=brief, result=str(result)
    )
    out = origin.news(cfg, thread, fake)
    (event,) = out["news"]
    assert event["kind"] == "finished"
    body = "**4,140 users** signed up in the window.\nNo data modified."
    assert event["result"] == body
    assert "secret" not in event["result"]
    (line,) = origin.render_news(out["news"])
    assert line.startswith(f"{ref} (Fix the thing): finished.\n**4,140 users**")
    assert not out["done"] and thread.tasks[ref]["result_told"]
    # The reaper lands it: nothing to merge, nothing new to say.
    fake.shows[ref] = show_text(
        ref, state="landed", outcome="not-applicable", brief=brief, result=str(result)
    )
    out = origin.news(cfg, thread, fake)
    assert out == {"news": [], "open": [], "done": True}


def test_news_reports_an_answer_only_task_that_landed_at_once(cfg, fake, tmp_path):
    ref = "t/01-count"
    thread = Thread(CHANNEL, TS, tasks={ref: {"state": "queued"}})
    brief = progress(tmp_path, ref, [])
    (Path(brief).parent / "result.md").write_text("The answer is 42.\n")
    fake.shows[ref] = show_text(
        ref, state="landed", outcome="not-applicable", brief=brief
    )
    out = origin.news(cfg, thread, fake)
    assert [e["kind"] for e in out["news"]] == ["finished"] and out["done"]
    assert out["news"][0]["result"] == "The answer is 42."
    assert "merged" not in origin.render_news(out["news"])[0]


def test_news_attaches_the_result_to_shipped_then_not_again(cfg, fake, tmp_path):
    ref = "t/01-a"
    thread = Thread(CHANNEL, TS, tasks={ref: {"state": "dispatched"}})
    brief = progress(tmp_path, ref, [])
    (Path(brief).parent / "result.md").write_text("---\no: 1\n---\nAdded it.\n")
    url = "https://example.test/pr/7"
    fake.shows[ref] = show_text(
        ref, state="done", outcome="shipped", artifact=url, brief=brief
    )
    out = origin.news(cfg, thread, fake)
    told = [(e["kind"], e.get("result")) for e in out["news"]]
    assert told == [("shipped", "Added it.")]
    fake.shows[ref] = show_text(
        ref, state="landed", outcome="shipped", artifact=url, brief=brief
    )
    out = origin.news(cfg, thread, fake)
    assert [(e["kind"], e.get("result")) for e in out["news"]] == [("landed", None)]


def test_result_body_is_capped_and_scrubbed(tmp_path):
    from vega import fleet as fleet_mod

    path = tmp_path / "result.md"
    path.write_text("x" * 5000)
    body = fleet_mod.result_body({"result": str(path)}, limit=100)
    assert body.startswith("x" * 100) and body.endswith("the full result is in fleet)")
    assert fleet_mod.result_body({"brief": str(tmp_path / "other" / "BRIEF.md")}) == ""
    assert fleet_mod.result_body({}) == ""


def test_news_reports_abandonment_and_a_pull_request_before_shipping(
    cfg, fake, tmp_path
):
    a, b = "t/01-a", "t/02-b"
    thread = Thread(CHANNEL, TS)
    fake.find_rows = [{"ref": a, "state": "dispatched", "session": "s", "host": None}]
    fake.shows[a] = show_text(
        a, state="dispatched", artifact="https://example.test/pr/1"
    )
    fake.shows[b] = show_text(
        b,
        state="abandoned",
        abandoned="abandoned by hand at 2026 (was queued) — superseded",
    )
    thread.tasks[b] = {
        "state": "queued",
        "outcome": "",
        "artifacts": [],
        "needs_input": "",
    }
    out = origin.news(cfg, thread, fake)
    kinds = {e["ref"]: e["kind"] for e in out["news"]}
    assert kinds == {a: "pull_request", b: "abandoned"}
    assert out["open"] == [a]
    lines = origin.render_news(out["news"])
    assert any("pull request opened" in line for line in lines)
    assert any("abandoned - abandoned by hand" in line for line in lines)


def test_news_tolerates_a_missing_record(cfg, fake):
    thread = Thread(CHANNEL, TS, tasks={"t/09-gone": {"state": "queued"}})
    with pytest.raises(FleetError):
        origin.news(cfg, thread, fake)


def test_send_window_prunes_old_entries(cfg, fake):
    thread = record(cfg)
    thread.sends["t/01-a"] = [time.time() - 7200, time.time() - 7100]
    fake.find_rows = [
        {"ref": "t/01-a", "state": "dispatched", "session": "s", "host": None}
    ]
    origin.send(cfg, thread, ref="t/01-a", answer="ok", runner=fake)
    assert len(thread.sends["t/01-a"]) == 1
