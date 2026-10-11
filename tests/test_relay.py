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
