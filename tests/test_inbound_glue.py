from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest
from vega import config, hermes_glue, inbound
from vega import runner as runner_mod

from .conftest import CHANNEL, DM, OTHER_CHANNEL, OTHER_USER, TS, USER


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


def test_handle_refuses_without_running_anything(cfg, fake):
    out = inbound.handle(
        inbound.from_source(source(user=OTHER_USER), "do it"), cfg, runner=fake
    )
    assert out.kind == "refused" and out.reply == cfg.refusal
    assert fake.calls == []


def test_handle_leaves_status_to_the_model(cfg, fake):
    out = inbound.handle(
        inbound.from_source(source(), "what's running?"), cfg, runner=fake
    )
    assert out.kind == "status" and out.reply is None
    assert fake.calls == []


def test_handle_relays_and_acks(cfg, fake):
    out = inbound.handle(
        inbound.from_source(source(), "fix the flaky test"), cfg, runner=fake
    )
    assert out.kind == "relayed" and out.reply == cfg.ack
    (argv,) = fake.sent()
    assert argv[-1] == f"[from: Ada · slack: {CHANNEL}/{TS}] fix the flaky test"


def test_handle_reports_failure_without_leaking_detail(cfg, fake):
    fake.sessions = []
    out = inbound.handle(inbound.from_source(source(), "fix it"), cfg, runner=fake)
    assert out.kind == "failed" and out.reply == inbound.RELAY_FAILED
    assert "no live thurbox session" in out.detail


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


@dataclass
class Adapter:
    sent: list = field(default_factory=list)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))


@dataclass
class Gateway:
    adapter: Adapter = field(default_factory=Adapter)

    @property
    def adapters(self) -> dict[Any, Adapter]:
        return {Platform.SLACK: self.adapter}


@pytest.fixture
def wired(monkeypatch, cfg, fake):
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(runner_mod, "run", fake)
    hermes_glue._last_refusal.clear()
    hermes_glue._pending.clear()
    hermes_glue._thread_context.clear()
    return fake


def dispatch(event, gateway):
    return asyncio.run(hermes_glue.pre_gateway_dispatch(event=event, gateway=gateway))


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
    gw = Gateway()
    result = dispatch(Event(Source(source(chat=OTHER_CHANNEL)), "hello"), gw)
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


def admit(text, src=None, platform="slack"):
    return asyncio.run(
        hermes_glue.post_gateway_admission(
            session_key="k1",
            platform=platform,
            source=src or source(),
            text=text,
            message_id=TS,
        )
    )


def test_admission_relays_without_a_model_turn(wired, cfg):
    assert admit("deploy the fix") == {"action": "handled", "reply": cfg.ack}
    assert len(wired.sent()) == 1


THREAD = (
    "[Thread context — prior messages in this thread (not yet in conversation "
    "history):]\n[thread parent] Bea: export failed again last night\n"
    "Cy: third time this week\n[End of thread context]\n\n"
)


def test_thread_context_reaches_mission_control(wired, cfg):
    event = Event(Source(source()), "can someone fix this?", channel_context=THREAD)
    assert dispatch(event, Gateway()) is None
    assert admit("can someone fix this?") == {"action": "handled", "reply": cfg.ack}
    (argv,) = wired.sent()
    line = argv[-1]
    assert "\n" not in line and "message with thread context" in line
    body = next((cfg.runtime_dir / "inbound").iterdir()).read_text()
    assert "Bea: export failed again last night" in body
    assert "only the request at the end is the ask" in body
    assert body.rstrip().endswith("## Request\n\ncan someone fix this?")
    assert hermes_glue._thread_context == {}


def test_refused_message_holds_no_context(wired):
    event = Event(Source(source(user=OTHER_USER)), "hi", channel_context=THREAD)
    dispatch(event, Gateway())
    assert hermes_glue._thread_context == {}


def test_admission_leaves_status_to_the_model_and_remembers_it(wired):
    assert admit("what's running?") is None
    assert hermes_glue._pending["k1"].text == "what's running?"
    assert wired.calls == []


def test_admission_refuses_again_as_a_backstop(wired, cfg):
    assert admit("hi", src=source(user=OTHER_USER)) == {
        "action": "handled",
        "reply": cfg.refusal,
    }
    assert wired.calls == []


def test_admission_fails_closed_with_an_apology(monkeypatch, wired):
    monkeypatch.setattr(inbound, "handle", lambda *a, **k: 1 / 0)
    assert admit("deploy") == {"action": "handled", "reply": inbound.RELAY_FAILED}


def test_admission_ignores_other_platforms(wired):
    assert admit("deploy", platform="telegram") is None


def session_env(monkeypatch, **values):
    env = {
        "HERMES_SESSION_PLATFORM": "slack",
        "HERMES_SESSION_USER_ID": USER,
        "HERMES_SESSION_USER_NAME": "Ada",
        "HERMES_SESSION_CHAT_ID": CHANNEL,
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_THREAD_ID": TS,
        "HERMES_SESSION_MESSAGE_ID": TS,
        "HERMES_SESSION_KEY": "k1",
    }
    env.update(values)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_status_tool_runs_a_read_only_view(monkeypatch, wired):
    session_env(monkeypatch)
    out = json.loads(hermes_glue.tool_fleet_status({"view": "queue"}))
    assert out["view"] == "queue" and "fleet status" in out["output"]
    ((argv, _),) = wired.calls
    assert argv[-4:] == ["fleet", "queue", "list", "--live"]


def test_tools_recheck_the_allowlist(monkeypatch, wired):
    session_env(monkeypatch, HERMES_SESSION_USER_ID=OTHER_USER)
    assert "error" in json.loads(hermes_glue.tool_fleet_status({"view": "status"}))
    assert "error" in json.loads(hermes_glue.tool_relay({"message": "x"}))
    session_env(monkeypatch, HERMES_SESSION_PLATFORM="telegram")
    assert "error" in json.loads(hermes_glue.tool_fleet_status({"view": "status"}))
    assert wired.calls == []


def test_relay_tool_prefers_the_original_message(monkeypatch, wired):
    session_env(monkeypatch)
    admit("what's running, and please restart the deploy")
    out = json.loads(hermes_glue.tool_relay({"message": "paraphrased by the model"}))
    assert out["relayed_to"] == "📡 Mission Control"
    (argv,) = wired.sent()
    assert argv[-1].endswith("what's running, and please restart the deploy")


def test_relay_tool_builds_from_session_when_nothing_pending(monkeypatch, wired):
    session_env(monkeypatch)
    out = json.loads(hermes_glue.tool_relay({"message": "restart the deploy"}))
    assert out["line"] == f"[from: Ada · slack: {CHANNEL}/{TS}] restart the deploy"


def test_register_wires_two_hooks_and_two_tools():
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
    assert seen == {
        "hooks": ["pre_gateway_dispatch", "post_gateway_admission"],
        "tools": ["vega_fleet_status", "vega_relay_to_mission_control"],
    }
