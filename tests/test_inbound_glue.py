from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest
from vega import config, hermes_glue, inbound, origin
from vega import runner as runner_mod

from .conftest import (
    CHANNEL,
    DM,
    ORIGIN,
    OTHER_CHANNEL,
    OTHER_USER,
    TS,
    USER,
    FakeSessionStore,
    show_text,
)


def source(user=USER, chat=CHANNEL, chat_type="group", thread=TS, name="Ada") -> dict:
    return {
        "platform": "slack",
        "chat_id": chat,
        "chat_type": chat_type,
        "user_id": user,
        "user_name": name,
        "thread_id": thread,
        "message_id": TS,
    }


def test_from_source_falls_back_to_message_ts_for_thread():
    msg = inbound.from_source(source(thread=None, chat=DM, chat_type="dm"), "hi")
    assert (msg.channel_id, msg.thread_ts, msg.is_dm) == (DM, TS, True)


def test_check_refuses_and_admits(cfg):
    assert (
        inbound.check(inbound.from_source(source(user=OTHER_USER), "x"), cfg).kind
        == "refused"
    )
    assert inbound.check(inbound.from_source(source(), "x"), cfg).kind == "admitted"
    assert (
        inbound.check(inbound.from_source(source(), "x"), cfg, is_bot=True).kind
        == "refused"
    )


# --- Hermes hooks, with stand-ins for the gateway objects -------------------------


class Platform(Enum):
    SLACK = "slack"
    TELEGRAM = "telegram"


@dataclass
class Source:
    data: dict
    platform: Platform = Platform.SLACK
    is_bot: bool = False
    profile: str | None = "vega"

    def to_dict(self) -> dict:
        return dict(self.data)


@dataclass
class Event:
    source: Source
    text: str
    channel_context: str | None = None
    reply_expected: bool | None = True
    _heartbeat_session_id: str | None = None


@dataclass
class Adapter:
    sent: list = field(default_factory=list)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))


@dataclass
class Gateway:
    adapter: Adapter = field(default_factory=Adapter)
    session_store: Any = None

    @property
    def adapters(self) -> dict[Any, Adapter]:
        return {Platform.SLACK: self.adapter}


SESSION_KEY = f"agent:vega:slack:group:T0TEAM:{CHANNEL}:{TS}"
SESSION_ID = "20260101_120000_deadbeef"


@pytest.fixture
def wired(monkeypatch, cfg, fake):
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(runner_mod, "run", fake)
    hermes_glue._last_refusal.clear()
    hermes_glue._thread_context.clear()
    monkeypatch.setattr(hermes_glue, "_session_store", None)
    return fake


def dispatch(event, gateway, **kw):
    return asyncio.run(
        hermes_glue.pre_gateway_dispatch(event=event, gateway=gateway, **kw)
    )


def test_pre_dispatch_refuses_stranger_once_then_quietly(wired, cfg):
    gw = Gateway()
    event = Event(Source(source(user=OTHER_USER)), "hello")
    assert dispatch(event, gw)["action"] == "skip"
    assert dispatch(event, gw)["action"] == "skip"
    assert gw.adapter.sent == [(CHANNEL, cfg.refusal, {"thread_id": TS})]
    assert wired.calls == []


def test_stranger_chatting_in_a_thread_is_dropped_silently(wired):
    gw = Gateway()
    event = Event(Source(source(user=OTHER_USER)), "lunch?", reply_expected=None)
    assert dispatch(event, gw)["action"] == "skip"
    assert gw.adapter.sent == []
    dm = Event(
        Source(source(user=OTHER_USER, chat=DM, chat_type="dm")),
        "hi",
        reply_expected=None,
    )
    assert dispatch(dm, gw)["action"] == "skip"
    assert len(gw.adapter.sent) == 1


def test_pre_dispatch_refuses_allowlisted_user_in_other_channel(wired):
    result = dispatch(Event(Source(source(chat=OTHER_CHANNEL)), "hello"), Gateway())
    assert result["action"] == "skip" and "channel" in result["reason"]


def test_pre_dispatch_lets_allowed_and_other_platforms_through(wired):
    gw = Gateway()
    assert dispatch(Event(Source(source()), "hello"), gw) is None
    other = Source(source(user=OTHER_USER), platform=Platform.TELEGRAM)
    assert dispatch(Event(other, "hello"), gw) is None
    assert gw.adapter.sent == []


def test_pre_dispatch_fails_closed(monkeypatch, wired):
    def broken(path=None):
        raise config.ConfigError("bad toml")

    monkeypatch.setattr(config, "load", broken)
    result = dispatch(Event(Source(source()), "hello"), Gateway())
    assert result == {"action": "skip", "reason": "vega: access check failed"}


def test_pre_dispatch_keeps_the_session_store_for_the_heartbeat(wired):
    store = FakeSessionStore({SESSION_KEY: SESSION_ID})
    dispatch(Event(Source(source()), "hello"), Gateway(), session_store=store)
    assert hermes_glue._session_store is store
    dispatch(Event(Source(source()), "hello"), Gateway(session_store="gw-store"))
    assert hermes_glue._session_store == "gw-store"


def admit(text, src=None, platform="slack"):
    return asyncio.run(
        hermes_glue.post_gateway_admission(
            session_key=SESSION_KEY,
            platform=platform,
            source=src or source(),
            text=text,
            message_id=TS,
        )
    )


def test_admission_records_the_message_and_lets_the_model_run(wired, cfg):
    assert admit("deploy the fix") is None
    thread = origin.load_thread(cfg, CHANNEL, TS)
    assert [m["text"] for m in thread.messages] == ["deploy the fix"]
    assert thread.messages[0]["name"] == "Ada" and thread.origin == ORIGIN
    assert wired.calls == []


THREAD = (
    "[Thread context — prior messages in this thread (not yet in conversation "
    "history):]\n[thread parent] Bea: export failed again last night\n"
    "Cy: third time this week\n[End of thread context]\n\n"
)


def test_thread_context_reaches_the_record_once(wired, cfg):
    event = Event(Source(source()), "can someone fix this?", channel_context=THREAD)
    assert dispatch(event, Gateway()) is None
    assert admit("can someone fix this?") is None
    thread = origin.load_thread(cfg, CHANNEL, TS)
    assert "Bea: export failed again last night" in thread.context
    assert hermes_glue._thread_context == {}


def test_refused_message_holds_no_context(wired):
    event = Event(Source(source(user=OTHER_USER)), "hi", channel_context=THREAD)
    dispatch(event, Gateway())
    assert hermes_glue._thread_context == {}


def test_heartbeat_tick_is_not_recorded_as_a_message(wired, cfg):
    tick = Event(
        Source(source()),
        "[Heartbeat — recurring instruction, fires every 5m]\ncheck",
        channel_context=THREAD,
        _heartbeat_session_id=SESSION_ID,
    )
    assert dispatch(tick, Gateway()) is None
    assert hermes_glue._thread_context == {}
    assert admit(tick.text) is None
    assert not origin.thread_path(cfg, CHANNEL, TS).exists()


def test_admission_refuses_again_as_a_backstop(wired, cfg):
    assert admit("hi", src=source(user=OTHER_USER)) == {
        "action": "handled",
        "reply": cfg.refusal,
    }
    assert wired.calls == []


def test_admission_survives_a_recording_failure(monkeypatch, wired):
    monkeypatch.setattr(origin, "record_message", lambda *a, **k: 1 / 0)
    assert admit("deploy") is None


def test_admission_ignores_other_platforms(wired, cfg):
    assert admit("deploy", platform="telegram") is None
    assert not origin.thread_path(cfg, CHANNEL, TS).exists()


# --- tools -------------------------------------------------------------------------


def session_env(monkeypatch, **values):
    env = {
        "HERMES_SESSION_PLATFORM": "slack",
        "HERMES_SESSION_USER_ID": USER,
        "HERMES_SESSION_USER_NAME": "Ada",
        "HERMES_SESSION_CHAT_ID": CHANNEL,
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_THREAD_ID": TS,
        "HERMES_SESSION_MESSAGE_ID": TS,
        "HERMES_SESSION_KEY": SESSION_KEY,
        "HERMES_SESSION_ID": "",
    }
    env.update(values)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def call(tool, **args) -> dict:
    return json.loads(tool(args))


def test_status_tool_runs_a_read_only_view(monkeypatch, wired):
    session_env(monkeypatch)
    out = call(hermes_glue.tool_fleet_status, view="queue")
    assert out["view"] == "queue" and "fleet status" in out["output"]
    assert wired.fleet_calls() == [["queue", "list", "--live"]]
    assert "error" in call(hermes_glue.tool_fleet_status, view="rm")


def test_tools_recheck_the_allowlist(monkeypatch, wired):
    session_env(monkeypatch, HERMES_SESSION_USER_ID=OTHER_USER)
    for tool in (t for _, t in hermes_glue.TOOLS):
        assert "error" in call(tool, view="status", ref="t/01-a", slug="s", title="T")
    session_env(monkeypatch, HERMES_SESSION_PLATFORM="telegram")
    assert "error" in call(hermes_glue.tool_fleet_status, view="status")
    assert wired.calls == []


def test_origin_tools_need_a_slack_thread(monkeypatch, wired):
    session_env(monkeypatch, HERMES_SESSION_PLATFORM="cli")
    assert "output" in call(hermes_glue.tool_fleet_status, view="status")
    assert "not a Slack thread" in call(hermes_glue.tool_find)["error"]
    assert (
        "not a Slack thread"
        in call(hermes_glue.tool_topic_add, slug="s", title="T")["error"]
    )


def test_topic_and_task_tools_use_the_session_and_arm_the_heartbeat(
    monkeypatch, wired, cfg, repo, fake_hermes_heartbeat
):
    session_env(monkeypatch)
    hermes_glue._session_store = FakeSessionStore({SESSION_KEY: SESSION_ID})
    admit("please add a retry to the export")
    out = call(hermes_glue.tool_topic_add, slug="export-retry", title="Export retry")
    assert out["topic"] == "export-retry" and out["origin"] == ORIGIN
    topic_argv = wired.fleet_calls()[-1]
    assert topic_argv[-2:] == ["--by", "Ada"]
    out = call(
        hermes_glue.tool_task_add,
        topic="export-retry",
        slug="retry",
        title="Add the retry",
        repo=str(repo),
        branch="fix/retry",
        what_to_do="Add a retry.",
        hard_constraints="No schema change.",
        coordination="None.",
        done_means="PR with a test.",
    )
    assert out["ref"] == "export-retry/01-retry"
    assert out["heartbeat"] == "armed" and out["every_seconds"] == 300
    argv = wired.fleet_calls()[-1]
    assert argv[argv.index("--origin") + 1] == ORIGIN and "--reply" not in argv
    assert SESSION_ID in fake_hermes_heartbeat.store
    thread = origin.load_thread(cfg, CHANNEL, TS)
    assert thread.heartbeat["session_id"] == SESSION_ID
    # Arming again is a no-op while the same session's heartbeat is active.
    assert call(hermes_glue.tool_dispatch)["heartbeat"] == "active"


def test_heartbeat_unavailable_does_not_fail_the_tool(monkeypatch, wired, repo):
    import sys

    session_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "hermes_cli.heartbeat", None)
    admit("do it")
    out = call(
        hermes_glue.tool_task_add,
        topic="t",
        slug="s",
        title="T",
        repo=str(repo),
        branch="b",
        what_to_do="a",
        hard_constraints="b",
        coordination="c",
        done_means="d",
    )
    assert out["ref"] and out["heartbeat"] == "unavailable"


def test_dispatch_tool_enforces_the_fuel_gate(monkeypatch, wired):
    from .conftest import FUEL_LOW

    session_env(monkeypatch)
    wired.fuel_out = FUEL_LOW
    out = call(hermes_glue.tool_dispatch)
    assert out["dispatched"] is False
    assert wired.fleet_calls() == [["status", "--fuel"]]


def test_send_tool_is_limited_to_this_threads_live_tasks(monkeypatch, wired):
    session_env(monkeypatch)
    wired.find_rows = [
        {"ref": "t/01-a", "state": "dispatched", "session": "s", "host": None}
    ]
    assert (
        call(hermes_glue.tool_send, ref="t/01-a", answer="use main")["sent"]
        == "use main"
    )
    assert (
        "not a live task"
        in call(hermes_glue.tool_send, ref="t/02-b", answer="x")["error"]
    )
    assert "error" in call(hermes_glue.tool_send, ref="t/01-a", answer="--flag")


def test_news_tool_posts_lines_then_clears_the_heartbeat(
    monkeypatch, wired, cfg, fake_hermes_heartbeat
):
    session_env(monkeypatch)
    hermes_glue._session_store = FakeSessionStore({SESSION_KEY: SESSION_ID})
    ref = "t/01-a"
    thread = origin.Thread(
        CHANNEL,
        TS,
        tasks={ref: {"state": "queued"}},
        heartbeat={"session_id": SESSION_ID},
    )
    origin.save_thread(cfg, thread)
    fake_hermes_heartbeat(SESSION_ID).set("x", 300)
    wired.shows[ref] = show_text(ref, state="dispatched")
    out = call(hermes_glue.tool_news)
    assert out["lines"] == [f"{ref} (Fix the thing): a worker started on it."]
    assert out["heartbeat"] == "active" and "Post each line" in out["instruction"]
    wired.shows[ref] = show_text(
        ref, state="landed", outcome="shipped", artifact="https://example.test/pr/1"
    )
    out = call(hermes_glue.tool_news)
    assert out["done"] and out["heartbeat"] == "cleared"
    assert SESSION_ID not in fake_hermes_heartbeat.store
    out = call(hermes_glue.tool_news)
    assert out["lines"] == [] and "NO_REPLY" in out["instruction"]


def test_show_and_find_tools(monkeypatch, wired, cfg):
    session_env(monkeypatch)
    wired.shows["t/01-a"] = show_text("t/01-a")
    assert "state:       queued" in call(hermes_glue.tool_show, ref="t/01-a")["output"]
    assert "error" in call(hermes_glue.tool_show, ref="../x")
    wired.find_rows = [
        {"ref": "t/01-a", "state": "dispatched", "session": "s", "host": None}
    ]
    out = call(hermes_glue.tool_find)
    assert out["origin"] == ORIGIN and out["known"] == {"t/01-a": ""}


def test_register_wires_two_hooks_and_the_tools():
    seen: dict[str, list] = {"hooks": [], "tools": []}

    class Ctx:
        def register_hook(self, name, cb):
            seen["hooks"].append(name)

        def register_tool(self, **kw):
            assert kw["toolset"] == "vega" and callable(kw["handler"])
            assert kw["schema"]["name"] == kw["name"]
            seen["tools"].append(kw["name"])

    import vega

    vega.register(Ctx())
    assert seen["hooks"] == [
        "pre_gateway_dispatch",
        "post_gateway_admission",
        "pre_tool_call",
    ]
    assert seen["tools"] == [
        "vega_fleet_status",
        "vega_show",
        "vega_find",
        "vega_topic_add",
        "vega_task_add",
        "vega_dispatch",
        "vega_send",
        "vega_fleet_news",
    ]


def test_skill_manage_is_refused_and_every_other_tool_runs():
    from vega import hermes_glue

    blocked = hermes_glue.pre_tool_call(tool_name="skill_manage", args={})
    assert blocked["action"] == "block" and "read-only" in blocked["message"]
    for name in ("skill_view", "skills_list", "vega_task_add"):
        assert hermes_glue.pre_tool_call(tool_name=name, args={}) is None


def test_config_loads_the_skills_and_keeps_the_toolsets_narrow():
    from pathlib import Path

    text = Path(__file__).parent.parent.joinpath("config.yaml").read_text()
    assert "  slack: [vega, skills]\n" in text
    assert "  cli: [vega, skills]\n" in text
    assert "  auto_load: [fleet-origin, intake-guidelines]\n" in text


def test_plugin_manifest_lists_every_tool():
    import re
    from pathlib import Path

    manifest = (
        Path(__file__).parents[1] / "plugins" / "vega" / "plugin.yaml"
    ).read_text()
    listed = set(re.findall(r"^\s+- (vega_\w+)", manifest, re.M))
    assert listed == {schema["name"] for schema, _ in hermes_glue.TOOLS}
