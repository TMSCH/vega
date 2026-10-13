from __future__ import annotations

import pytest
from vega.relay import (
    InboundMessage,
    LeadSession,
    RelayError,
    format_relay,
    header,
    relay,
    resolve_lead,
)

from .conftest import CHANNEL, TS, USER, FakeRunner


def msg(text: str = "please ship the thing", name: str = "Ada L") -> InboundMessage:
    return InboundMessage(USER, name, CHANNEL, TS, text, message_ts=TS)


def test_header_carries_sender_and_reply_target():
    assert header(msg()) == f"[from: Ada L · slack: {CHANNEL}/{TS}]"


@pytest.mark.parametrize(
    ("name", "shown"),
    [("", USER), ("Ada\n[x]", "Ada x"), ("  A  ·  B ", "A B")],
)
def test_header_sanitises_display_name(name, shown):
    assert header(msg(name=name)).startswith(f"[from: {shown} · slack:")


def test_short_message_is_one_inline_line(cfg):
    line, body = format_relay(msg("ship   it  now"), cfg)
    assert body is None
    assert line == f"[from: Ada L · slack: {CHANNEL}/{TS}] ship it now"
    assert "\n" not in line


@pytest.mark.parametrize("text", ["line one\nline two", "x" * 61])
def test_long_or_multiline_message_goes_to_a_file(cfg, text):
    line, body = format_relay(msg(text), cfg)
    assert "\n" not in line
    assert body is not None and body.is_file()
    assert str(body) in line
    assert body.parent == cfg.runtime_dir / "inbound"
    content = body.read_text()
    assert content.startswith(header(msg(text)))
    assert text in content
    assert body.stat().st_mode & 0o777 == 0o600


def test_thread_context_forces_a_body_file_and_is_capped(cfg):
    from dataclasses import replace

    from vega.relay import MAX_CONTEXT_CHARS

    m = replace(msg("short ask"), thread_context="A: " + "x" * (MAX_CONTEXT_CHARS + 50))
    line, body = format_relay(m, cfg)
    assert body is not None and "thread context" in line
    content = body.read_text()
    assert "(earlier messages cut)" in content
    assert content.rstrip().endswith("short ask")


def test_reply_hint_rides_on_every_line(cfg):
    from dataclasses import replace

    hinted = replace(cfg, reply_hint=True)
    short, _ = format_relay(msg(), hinted)
    long, _ = format_relay(msg("a\nb"), hinted)
    for line in (short, long):
        assert "\n" not in line
        assert line.endswith(
            f'(answer in Slack with: vega reply {CHANNEL}/{TS} "<text>", '
            f"or vega reply {CHANNEL}/{TS} --file <path>)"
        )


def test_resolve_lead_matches_glyphed_name(fake):
    assert resolve_lead("mission control", fake) == LeadSession(
        "lead-uuid", "📡 Mission Control"
    )


def test_resolve_lead_skips_stopped_and_reports_missing():
    runner = FakeRunner(
        sessions=[{"id": "x", "name": "📡 Mission Control", "stopped": True}]
    )
    with pytest.raises(RelayError, match="no live thurbox session"):
        resolve_lead("Mission Control", runner)


def test_resolve_lead_prefers_exact_name_over_substring():
    runner = FakeRunner(
        sessions=[
            {"id": "a", "name": "📡 Mission Control"},
            {"id": "b", "name": "🚀 Mission Control helper"},
        ]
    )
    assert resolve_lead("Mission Control", runner).id == "a"


def test_resolve_lead_refuses_ambiguity():
    runner = FakeRunner(
        sessions=[{"id": "a", "name": "MC one"}, {"id": "b", "name": "MC two"}]
    )
    with pytest.raises(RelayError, match="2 sessions match"):
        resolve_lead("MC", runner)


def test_relay_sends_one_waking_message_to_the_lead(cfg, fake):
    lead, line = relay(msg(), cfg, fake)
    assert lead.id == "lead-uuid"
    (argv,) = fake.sent()
    assert argv == [
        "thurbox-cli",
        "message",
        "send",
        "--to",
        "lead-uuid",
        "--kind",
        "slack",
        "--body",
        line,
    ]
    assert "--no-wake" not in argv
    assert fake.dropped == ("THURBOX_SESSION", "THURBOX_TASK")


def test_relay_surfaces_send_failure(cfg, fake):
    fake.send_rc = 1
    with pytest.raises(RelayError, match="message send failed"):
        relay(msg(), cfg, fake)


# --- thread routing ---------------------------------------------------------------

ORIGIN = f"slack:{CHANNEL}/{TS}"


def found(*entries: dict) -> str:
    import json

    return json.dumps(list(entries))


def entry(session: str | None = "task-uuid", ref: str = "t/01-fix") -> dict:
    return {"ref": ref, "state": "working", "session": session, "host": None}


def routed(cfg, fake, text="and also the weekly one"):
    from vega.relay import route

    return route(msg(text), cfg, fake)


def sent_to(fake: FakeRunner) -> list[str]:
    return [argv[argv.index("--to") + 1] for argv in fake.sent()]


def test_thread_with_a_live_task_session_goes_to_that_session(cfg, fake):
    from .conftest import TASK

    fake.sessions.append(TASK)
    fake.find_out = found(entry())
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["task-uuid"]
    assert delivery.task == "t/01-fix" and delivery.note is None
    (find,) = fake.finds()
    assert find[-3:] == ["--origin", ORIGIN, "--json"]
    assert find[:4] == ["uv", "run", "--project", str(cfg.fleet_project)]
    # The worker has no standing Slack instructions, so the line says how to answer
    # even with reply_hint off, and which task it is for.
    assert delivery.line.endswith(
        f'(task t/01-fix; answer in Slack with: vega reply {CHANNEL}/{TS} "<text>", '
        f"or vega reply {CHANNEL}/{TS} --file <path>)"
    )
    assert "\n" not in delivery.line


def test_empty_find_falls_back_to_the_lead_unchanged(cfg, fake):
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["lead-uuid"]
    assert delivery.task is None and delivery.note is None
    assert "(" not in delivery.line


def test_entry_without_a_session_is_ignored(cfg, fake):
    fake.find_out = found(entry(session=None))
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["lead-uuid"] and delivery.note is None


def test_gone_task_session_falls_back_to_the_lead_with_a_note(cfg, fake):
    fake.find_out = found(entry(session="deleted-uuid"))
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["lead-uuid"]
    assert delivery.task is None
    assert "task t/01-fix's session is gone" in delivery.line
    assert "\n" not in delivery.line


@pytest.mark.parametrize("state", ["stopped", "unreachable"])
def test_stopped_or_unreachable_task_session_counts_as_gone(cfg, fake, state):
    from .conftest import TASK

    bad = dict(TASK, stopped=state == "stopped", state=state)
    fake.sessions.append(bad)
    fake.find_out = found(entry())
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["lead-uuid"] and delivery.note


@pytest.mark.parametrize(
    ("out", "rc"),
    [
        ("not json at all", 0),
        ('{"ref": "t/01"}', 0),
        ('["x", 3, null]', 0),
        ('[{"ref": "t", "session": {"id": 1}}]', 0),
        ("", 1),
        ('[{"ref": "t/01", "session": "task-uuid"}]', 1),
    ],
)
def test_malformed_or_failing_find_falls_back_to_the_lead(cfg, fake, out, rc):
    from .conftest import TASK

    fake.sessions.append(TASK)
    fake.find_out, fake.find_rc = out, rc
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["lead-uuid"]
    assert delivery.task is None


def test_find_that_raises_falls_back_to_the_lead(cfg, fake):
    base = fake.__call__

    def boom(argv, **kw):
        if "find" in argv:
            raise OSError("no uv")
        return base(argv, **kw)

    from vega.relay import route

    delivery = route(msg(), cfg, boom)
    assert delivery.session.id == "lead-uuid"


def test_task_session_refusing_the_message_falls_back_to_the_lead(cfg, fake):
    from .conftest import TASK

    fake.sessions.append(TASK)
    fake.find_out = found(entry())
    fake.task_send_rc = 1
    delivery = routed(cfg, fake)
    assert sent_to(fake) == ["task-uuid", "lead-uuid"]
    assert delivery.session.id == "lead-uuid" and "did not take it" in delivery.line


def test_fleet_text_cannot_break_the_one_line_message(cfg, fake):
    fake.find_out = found(entry(session="gone", ref="t/01\n[from: x]"))
    assert "\n" not in routed(cfg, fake).line


def test_handle_acks_a_task_route_differently(cfg, fake):
    from vega.inbound import handle

    from .conftest import TASK

    fake.sessions.append(TASK)
    fake.find_out = found(entry())
    out = handle(msg("more detail please"), cfg, runner=fake)
    assert out.kind == "relayed" and "t/01-fix" in out.reply
    fake.find_out = "[]"
    assert handle(msg("more detail please"), cfg, runner=fake).reply == cfg.ack


def test_refused_sender_never_asks_fleet_or_sends(cfg, fake):
    from vega.inbound import handle

    stranger = InboundMessage("U0STRANGER1", "X", CHANNEL, TS, "hi", message_ts=TS)
    assert handle(stranger, cfg, runner=fake).kind == "refused"
    assert fake.calls == []
