"""Offline end-to-end run of Vega as a fleet origin, as real as it gets without Slack.

Run it with the gateway's own Python (so Hermes' modules import) against a throwaway
HERMES_HOME and a scratch fleet queue; it never touches the live profile, the
running gateway, Slack or the real queue:

    DEMO=/tmp/vega-demo HERMES_HOME=$DEMO/hermes-home HERMES_DISABLE_LAZY_INSTALLS=1 \\
    VEGA_CONFIG=$DEMO/vega.toml FLEET_QUEUE_DIR=$DEMO/queue FLEET_RUNS_DIR=$DEMO/runs \\
    <hermes venv>/bin/python scripts/demo_offline.py

with ``$DEMO/repo`` a git checkout and ``$DEMO/vega.toml`` allowlisting U0DEMOUSER1 in
C0DEMOCHAN1, ``fleet.repos = ["$DEMO/repo"]`` and ``runtime.dir = "$DEMO/state"``.
``HERMES_AGENT`` names the hermes-agent checkout (default ``~/.hermes/hermes-agent``).
``HERMES_DISABLE_LAZY_INSTALLS=1`` stops Hermes from building a runtime for the
throwaway home and re-launching this script under it.

Real: Hermes' SessionStore, session ContextVars, HeartbeatManager, the gateway's
heartbeat restore scan and poll step; fleet's ``topic add``, ``add``, ``find``,
``show``, ``declare`` and ``status --fuel`` against the scratch queue; Vega's hooks,
tools and files. Stubbed: ``fleet queue dispatch`` runs with ``--dry-run`` (it would
spawn thurbox sessions), ``fleet queue send`` is recorded instead of run (it would
type into a session), the Slack adapter is a capture object, no model turn runs
(the tools are called directly), and the worker's state changes are edits to the
scratch task.yaml.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

DEMO = Path(os.environ["DEMO"])
HERMES_AGENT = Path(
    os.environ.get("HERMES_AGENT", "~/.hermes/hermes-agent")
).expanduser()
sys.path.insert(0, str(HERMES_AGENT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

import vega.runner as vrunner  # noqa: E402
from gateway.config import GatewayConfig, Platform  # noqa: E402
from gateway.platforms.event import MessageEvent  # noqa: E402
from gateway.run import GatewayRunner  # noqa: E402
from gateway.run_heartbeat_restore import restore_heartbeat_watches  # noqa: E402
from gateway.session import SessionSource, SessionStore  # noqa: E402
from gateway.session_context import get_session_env, set_session_vars  # noqa: E402
from hermes_cli.heartbeat import load_heartbeat, save_heartbeat  # noqa: E402
from vega import config, hermes_glue, origin  # noqa: E402

CHANNEL, TS, USER = "C0DEMOCHAN1", "1700000000.000100", "U0DEMOUSER1"
LOG: list[str] = []
ARGV_LOG: list[list[str]] = []
REAL_RUN = vrunner.run


def say(line: str) -> None:
    print(line)
    LOG.append(line)


def check(cond: bool, what: str) -> None:
    say(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def demo_run(argv, *, input=None, timeout=30.0, drop_env=()):
    """Every fleet command, real, except the two that would reach thurbox sessions."""
    argv = list(argv)
    tail = argv[5:]
    if tail[:2] == ["queue", "dispatch"]:
        argv = [*argv, "--dry-run"]
    ARGV_LOG.append(argv[5:])
    if tail[:2] == ["queue", "send"]:
        return vrunner.Completed(0, f"send: {tail[2]} -> (stubbed)  delivered\n", "")
    return REAL_RUN(argv, input=input, timeout=timeout, drop_env=drop_env)


def tool(fn, **args):
    out = json.loads(fn(args))
    say(f"  {fn.__name__}({', '.join(args)}) -> {json.dumps(out)[:300]}")
    return out


class CaptureAdapter:
    _message_handler = True

    def __init__(self):
        self._active_sessions: dict = {}
        self._session_tasks: dict = {}
        self._pending_messages: dict = {}
        self.events: list = []

    async def handle_message(self, event):
        self.events.append(event)


def bare_runner(store, gw_config, adapter):
    """The slice of GatewayRunner the restore scan and poll step read."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = gw_config
    runner.session_store = store
    runner._heartbeat_watch = {}
    runner._start_heartbeat_poller = lambda: None
    runner._profile_name_for_source = lambda source, adapter_profile=None: (
        source.profile
    )
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._profile_scope_for_source = lambda source: contextlib.nullcontext()
    runner._is_session_running = lambda key: False
    runner._queue_depth = lambda key, adapter=None: 0
    runner._delivery_adapter_for = lambda source: adapter
    return runner


def main() -> int:
    vrunner.run = demo_run
    home = Path(os.environ["HERMES_HOME"])
    gw_config = GatewayConfig()
    store = SessionStore(home / "sessions", gw_config)
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id=CHANNEL,
        chat_type="group",
        thread_id=TS,
        user_id=USER,
        user_name="Demo Teammate",
        scope_id="T0DEMOTEAM",
        message_id=TS,
    )
    entry = store.get_or_create_session(source)
    say(f"session key {entry.session_key!r} -> id {entry.session_id}")

    # Bind the ContextVars the way gateway/run.py binds them for a turn: no session_id.
    set_session_vars(
        platform="slack",
        chat_id=CHANNEL,
        chat_type="group",
        thread_id=TS,
        user_id=USER,
        user_name="Demo Teammate",
        scope_id="T0DEMOTEAM",
        session_key=entry.session_key,
        message_id=TS,
    )
    check(
        get_session_env("HERMES_SESSION_ID") == "",
        "HERMES_SESSION_ID reads empty on a bound turn (why the store lookup exists)",
    )
    check(
        get_session_env("HERMES_SESSION_KEY") == entry.session_key, "session key bound"
    )
    cfg = config.load()
    say(f"vega config from {cfg.source}; runtime {cfg.runtime_dir}")

    def through_hooks(text: str, **extra):
        event = SimpleNamespace(
            source=source, text=text, channel_context=None, reply_expected=True, **extra
        )
        pre = asyncio.run(
            hermes_glue.pre_gateway_dispatch(
                event=event, gateway=None, session_store=store
            )
        )
        post = asyncio.run(
            hermes_glue.post_gateway_admission(
                session_key=entry.session_key,
                platform="slack",
                source=source.to_dict(),
                text=text,
                message_id=TS,
            )
        )
        return pre, post

    # 1. A teammate's message, then a stranger's.
    outcome = through_hooks(
        "Please add a retry to the nightly export job; keep the job name."
    )
    check(
        outcome == (None, None),
        "hooks admit the teammate's message and leave it to the model",
    )
    check(
        hermes_glue._session_store is store,
        "pre_gateway_dispatch kept the session store",
    )
    thread = origin.load_thread(cfg, CHANNEL, TS)
    check(
        thread.messages[0]["text"].startswith("Please add a retry"),
        "message recorded verbatim",
    )
    stranger = SimpleNamespace(
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id=CHANNEL,
            chat_type="group",
            thread_id=TS,
            user_id="U0STRANGER1",
            message_id=TS,
        ),
        text="hi",
        channel_context=None,
        reply_expected=True,
    )
    pre = asyncio.run(
        hermes_glue.pre_gateway_dispatch(
            event=stranger, gateway=None, session_store=store
        )
    )
    check(
        pre == {"action": "skip", "reason": "vega: user not on the allowlist"},
        "stranger dropped",
    )

    # 2. Intake through the tools: real fleet commands into the scratch queue.
    out = tool(
        hermes_glue.tool_topic_add,
        slug="demo-nightly-retry",
        title="Nightly export retry",
    )
    check(out.get("topic") == "demo-nightly-retry", "real `fleet queue topic add`")
    topic_dir = DEMO / "queue" / "demo-nightly-retry"
    check(
        "Please add a retry to the nightly export job"
        in (topic_dir / "PROMPT.md").read_text(),
        "PROMPT.md verbatim",
    )
    check(
        "Demo Teammate" in (topic_dir / "topic.yaml").read_text(),
        "topic records the requester",
    )
    out = tool(
        hermes_glue.tool_task_add,
        topic="demo-nightly-retry",
        slug="add-retry",
        title="Add a retry to the nightly export",
        repo=str(DEMO / "repo"),
        branch="fix/nightly-retry",
        what_to_do="Wrap the nightly export job in a bounded retry (3 attempts).",
        hard_constraints="No schema changes. Keep the job name.",
        coordination="Nothing else touches the exporter.",
        done_means="A pull request with a test that exercises the retry.",
    )
    ref = out.get("ref", "")
    check(
        ref == "demo-nightly-retry/01-add-retry",
        "real `fleet queue add` returned the ref",
    )
    task_yaml = topic_dir / "01-add-retry" / "task.yaml"
    text = task_yaml.read_text()
    check(
        f"origin: slack:{CHANNEL}/{TS}" in text,
        "task.yaml records the thread as the origin",
    )
    check(
        "reply:" not in text or "reply: null" in text or "reply: ''" in text,
        "no --reply recorded",
    )
    check(out.get("heartbeat") == "armed", "task add armed the heartbeat")
    bad = tool(
        hermes_glue.tool_task_add,
        topic="demo-nightly-retry",
        slug="evil",
        title="x",
        repo="/etc",
        branch="b",
        what_to_do="a",
        hard_constraints="b",
        coordination="c",
        done_means="d",
    )
    check(
        "not allowlisted" in bad.get("error", ""), "a repo off the allowlist is refused"
    )
    out = tool(hermes_glue.tool_dispatch)
    check(
        "fuel" in out.get("reason", ""),
        f"fuel gate read real fuel: {out.get('reason')}",
    )
    ran_dry = any(a[:2] == ["queue", "dispatch"] and "--dry-run" in a for a in ARGV_LOG)
    check(
        ran_dry or not out["dispatched"], "dispatch ran --dry-run (stubbed) or was held"
    )

    # 3. The heartbeat row Hermes polls for, found by the gateway's own restore scan.
    state = load_heartbeat(entry.session_id)
    check(
        state is not None
        and state.status == "active"
        and state.interval_seconds == 300,
        "heartbeat row exists for the routed session id, every 300s",
    )
    check(
        "vega_fleet_news" in state.prompt,
        "the prompt tells the model to call vega_fleet_news",
    )
    adapter = CaptureAdapter()
    runner = bare_runner(store, gw_config, adapter)
    asyncio.run(restore_heartbeat_watches(runner))
    watch = runner._heartbeat_watch.get(entry.session_key, (None, None))
    check(
        watch[1] == entry.session_id,
        "restore scan registered the watch (the poller runs it every 5s)",
    )

    # 4. A due tick goes through the poller's own step, then Vega's hooks.
    state.created_at = time.time() - 600
    save_heartbeat(entry.session_id, state)
    asyncio.run(
        runner._heartbeat_poll_watch(
            runner._heartbeat_watch, entry.session_key, source, entry.session_id
        )
    )
    check(len(adapter.events) == 1, "poll step fired one tick into the adapter")
    tick = adapter.events[0]
    check(
        isinstance(tick, MessageEvent)
        and tick.text.startswith("[Heartbeat — ")
        and "vega_fleet_news" in tick.text,
        "the tick is a user-role event carrying Vega's prompt",
    )
    check(
        getattr(tick, "_heartbeat_session_id", None) == entry.session_id
        and not tick.internal,
        "tick is non-internal (hooks apply) and marked as a heartbeat",
    )
    check(
        through_hooks(tick.text, _heartbeat_session_id=entry.session_id)
        == (None, None),
        "hooks pass the tick to the model",
    )
    check(
        len(origin.load_thread(cfg, CHANNEL, TS).messages) == 1,
        "the tick was not recorded as a message",
    )

    # 5. News over fleet's real records as the task moves.
    out = tool(hermes_glue.tool_news)
    check(out["news"] == [] and "NO_REPLY" in out["instruction"], "no news -> NO_REPLY")
    task_yaml.write_text(
        task_yaml.read_text().replace("state: queued", "state: dispatched")
        + "session: 00000000-0000-4000-8000-00000000demo\nprompted: true\n"
    )
    out = tool(hermes_glue.tool_news)
    check([e["kind"] for e in out["news"]] == ["started"], "news: started")
    declare = REAL_RUN(
        [
            "uv",
            "run",
            "--project",
            str(cfg.fleet_project),
            "fleet",
            "queue",
            "declare",
            ref,
            "needs-input",
            "--why",
            "Retry 3 or 5 times?",
        ],
        timeout=60,
    )
    check(
        declare.returncode == 0,
        f"real `fleet queue declare needs-input`: {declare.stdout.strip()}",
    )
    label = f"{ref} (Add a retry to the nightly export)"
    out = tool(hermes_glue.tool_news)
    check(
        out["lines"] == [f"{label}: the worker needs input - Retry 3 or 5 times?"],
        "news: the question, once",
    )
    check(
        tool(hermes_glue.tool_news)["news"] == [], "the same question is not repeated"
    )
    out = tool(hermes_glue.tool_send, ref=ref, answer="3 times, then fail loudly")
    check(
        out.get("sent") == "3 times, then fail loudly"
        and ARGV_LOG[-1][:3] == ["queue", "send", ref],
        "vega_send accepted a live ref (send stubbed)",
    )
    out = tool(hermes_glue.tool_send, ref="demo-nightly-retry/02-nope", answer="x")
    check(
        "not a live task" in out.get("error", ""),
        "vega_send refuses a ref this thread does not own",
    )
    task_yaml.write_text(
        task_yaml.read_text().replace("state: dispatched", "state: done")
        + "outcome: shipped\nartifact: https://example.invalid/pr/42\n"
    )
    out = tool(hermes_glue.tool_news)
    check(
        out["lines"] == [f"{label}: shipped - https://example.invalid/pr/42."],
        "news: shipped with the PR URL",
    )
    task_yaml.write_text(task_yaml.read_text().replace("state: done", "state: landed"))
    out = tool(hermes_glue.tool_news)
    check(out["lines"] == [f"{label}: landed (merged)."], "news: landed")
    check(
        out["done"] and out["heartbeat"] == "cleared",
        "thread finished: heartbeat cleared",
    )
    check(
        load_heartbeat(entry.session_id) is None,
        "heartbeat row cleared in Hermes' store",
    )
    runner2 = bare_runner(store, gw_config, adapter)
    asyncio.run(restore_heartbeat_watches(runner2))
    check(runner2._heartbeat_watch == {}, "restore scan registers nothing once cleared")

    say("")
    say("fleet commands run (argv after `uv run --project <fleet> fleet`):")
    for a in ARGV_LOG:
        say(
            "  "
            + " ".join(a).replace(str(DEMO), "<demo>").replace(str(Path.home()), "~")
        )
    check(
        all(a[0] in {"queue", "status"} for a in ARGV_LOG),
        "only queue/status verbs ran",
    )
    check(
        not any(a[:2] in (["queue", "abandon"], ["queue", "block"]) for a in ARGV_LOG),
        "no abandon or block ran",
    )
    store.close_all_db_handles()
    (DEMO / "transcript.txt").write_text("\n".join(LOG) + "\n")
    say("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
