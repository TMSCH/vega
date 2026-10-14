from __future__ import annotations

import pytest
from vega import heartbeat

from .conftest import FakeSessionStore


def test_unavailable_outside_hermes(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hermes_cli.heartbeat", None)
    with pytest.raises(heartbeat.HeartbeatUnavailable):
        heartbeat.arm("sid", 300)
    assert heartbeat.is_active("sid") is False


def test_arm_writes_the_row_hermes_polls(fake_hermes_heartbeat):
    out = heartbeat.arm("20260101_000000_abcd1234", 300)
    assert out == {
        "session_id": "20260101_000000_abcd1234",
        "interval_seconds": 300,
        "status": "active",
    }
    state = fake_hermes_heartbeat.store["20260101_000000_abcd1234"]
    assert state.prompt == heartbeat.PROMPT and "NO_REPLY" in state.prompt
    assert heartbeat.is_active("20260101_000000_abcd1234")
    assert "300s" in heartbeat.status_line("20260101_000000_abcd1234")
    assert heartbeat.clear("20260101_000000_abcd1234") is True
    assert heartbeat.clear("20260101_000000_abcd1234") is False
    assert not heartbeat.is_active("20260101_000000_abcd1234")


def test_arm_refuses_an_empty_session_or_a_short_interval(fake_hermes_heartbeat):
    with pytest.raises(heartbeat.HeartbeatUnavailable):
        heartbeat.arm("", 300)
    with pytest.raises(ValueError):
        heartbeat.arm("sid", 30)


def test_session_id_comes_from_the_store_by_key_then_the_env():
    store = FakeSessionStore({"agent:vega:slack:group:T:C:1.2": "sid-from-store"})
    assert (
        heartbeat.session_id_for("agent:vega:slack:group:T:C:1.2", store, "env")
        == "sid-from-store"
    )
    assert heartbeat.session_id_for("other-key", store, "env-sid") == "env-sid"
    assert heartbeat.session_id_for("", store, "") == ""
    assert heartbeat.session_id_for("k", None, "env-sid") == "env-sid"

    class Broken:
        def peek_session_id(self, key):
            raise RuntimeError("cold index")

    assert heartbeat.session_id_for("k", Broken(), "env-sid") == "env-sid"
