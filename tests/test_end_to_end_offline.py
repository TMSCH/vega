"""The whole path, offline: Slack message -> hooks -> intake tools -> heartbeat -> news.

Everything a real run would touch is stubbed the way the other tests stub it: fleet is
the scripted FakeRunner, Hermes' heartbeat store is the fake module, the session store
is a dict. What is real is Vega's own code end to end, including the files it writes.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from vega import config, hermes_glue, origin
from vega import runner as runner_mod

from .conftest import CHANNEL, ORIGIN, TS, USER, FakeSessionStore, show_text
from .test_inbound_glue import (
    SESSION_ID,
    SESSION_KEY,
    Event,
    Gateway,
    Source,
    session_env,
    source,
)

HEARTBEAT_TICK = (
    "[Heartbeat — recurring instruction, fires every 5m]\n"
    + hermes_glue.heartbeat.PROMPT
)


@pytest.fixture
def world(monkeypatch, cfg, fake, fake_hermes_heartbeat):
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(runner_mod, "run", fake)
    hermes_glue._thread_context.clear()
    hermes_glue._last_refusal.clear()
    monkeypatch.setattr(hermes_glue, "_session_store", None)
    session_env(monkeypatch)
    return fake


def inbound(text: str, store):
    """One Slack message through both hooks, as the gateway would run them."""
    event = Event(Source(source()), text)
    gate = Gateway(session_store=store)
    pre = asyncio.run(
        hermes_glue.pre_gateway_dispatch(event=event, gateway=gate, session_store=store)
    )
    post = asyncio.run(
        hermes_glue.post_gateway_admission(
            session_key=SESSION_KEY,
            platform="slack",
            source=source(),
            text=text,
            message_id=TS,
        )
    )
    return pre, post


def tool(fn, **args):
    return json.loads(fn(args))


def test_thread_becomes_an_origin_and_hears_back(
    world, cfg, repo, fake_hermes_heartbeat, tmp_path
):
    store = FakeSessionStore({SESSION_KEY: SESSION_ID})

    # 1. A teammate asks in a thread. Both hooks let it through to the model.
    assert inbound("please add a retry to the nightly export", store) == (None, None)
    thread = origin.load_thread(cfg, CHANNEL, TS)
    assert thread.messages[0]["user"] == USER

    # 2. The model runs the intake with the tools; the handler fixes the origin.
    out = tool(
        hermes_glue.tool_topic_add, slug="nightly-retry", title="Nightly export retry"
    )
    assert out["topic"] == "nightly-retry"
    out = tool(
        hermes_glue.tool_task_add,
        topic="nightly-retry",
        slug="add-retry",
        title="Add a retry to the nightly export",
        repo=str(repo),
        branch="fix/nightly-retry",
        what_to_do="Add a bounded retry around the export job.",
        hard_constraints="No schema changes; keep the job name.",
        coordination="Nothing else touches the exporter.",
        done_means="A pull request with a test that exercises the retry.",
    )
    ref = out["ref"]
    assert ref == "nightly-retry/01-add-retry" and out["heartbeat"] == "armed"
    add_argv = world.fleet_calls()[-1]
    assert (
        add_argv[add_argv.index("--origin") + 1] == ORIGIN and "--reply" not in add_argv
    )

    # 3. Fuel is clearly above the reserve, so the thread dispatches its own task.
    out = tool(hermes_glue.tool_dispatch)
    assert out["dispatched"] is True
    assert world.fleet_calls()[-1] == ["queue", "dispatch", "--origin", ORIGIN]

    # 4. The heartbeat row Hermes polls for exists for the routed session id.
    assert fake_hermes_heartbeat(SESSION_ID).is_active()
    assert "vega_fleet_news" in fake_hermes_heartbeat.store[SESSION_ID].prompt

    # 5. A tick arrives as a normal turn: the hooks pass it, record nothing.
    world.find_rows = [
        {"ref": ref, "state": "dispatched", "session": "sid", "host": None}
    ]
    brief = tmp_path / "queue" / ref / "BRIEF.md"
    brief.parent.mkdir(parents=True)
    world.shows[ref] = show_text(
        ref, state="dispatched", brief=str(brief), title="Add a retry"
    )
    assert inbound(HEARTBEAT_TICK, store) == (None, None)
    assert len(origin.load_thread(cfg, CHANNEL, TS).messages) == 1
    news = tool(hermes_glue.tool_news)
    assert news["lines"] == [f"{ref} (Add a retry): a worker started on it."]

    # 6. Next tick: nothing changed -> the model is told to answer NO_REPLY.
    news = tool(hermes_glue.tool_news)
    assert news["news"] == [] and "NO_REPLY" in news["instruction"]

    # 7. The worker declares needs-input; the thread hears the question and answers.
    (brief.parent / "progress.jsonl").write_text(
        json.dumps(
            {
                "declare": {"state": "needs-input", "why": "retry 3 or 5 times?"},
                "observed": "2026-10-14T06:00:00+00:00",
            }
        )
        + "\n"
    )
    news = tool(hermes_glue.tool_news)
    assert news["lines"] == [
        f"{ref} (Add a retry): the worker needs input - retry 3 or 5 times?"
    ]
    assert tool(hermes_glue.tool_send, ref=ref, answer="3 times, then fail loudly")[
        "sent"
    ]
    assert world.fleet_calls()[-1] == [
        "queue",
        "send",
        ref,
        "3 times, then fail loudly",
    ]

    # 8. Shipped with a PR, then landed: two reports, then the heartbeat is cleared.
    world.shows[ref] = show_text(
        ref,
        state="done",
        outcome="shipped",
        artifact="https://example.test/pr/42",
        brief=str(brief),
        title="Add a retry",
    )
    news = tool(hermes_glue.tool_news)
    assert news["lines"] == [
        f"{ref} (Add a retry): shipped - https://example.test/pr/42."
    ]
    world.find_rows = []
    world.shows[ref] = show_text(
        ref,
        state="landed",
        outcome="shipped",
        artifact="https://example.test/pr/42",
        brief=str(brief),
        title="Add a retry",
    )
    news = tool(hermes_glue.tool_news)
    assert news["lines"] == [f"{ref} (Add a retry): landed (merged)."]
    assert news["done"] and news["heartbeat"] == "cleared"
    assert not fake_hermes_heartbeat(SESSION_ID).is_active()

    # Nothing ever ran but fixed fleet argv, with the minimal environment.
    verbs = {tuple(a[:2]) for a in world.fleet_calls()}
    assert verbs <= {
        ("status", "--fuel"),
        ("queue", "topic"),
        ("queue", "add"),
        ("queue", "dispatch"),
        ("queue", "find"),
        ("queue", "show"),
        ("queue", "send"),
    }
    assert world.dropped == ("SLACK_*", "THURBOX_SESSION", "THURBOX_TASK")
