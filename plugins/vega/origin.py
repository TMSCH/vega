"""A Slack thread as a fleet origin: ``slack:<channel>/<thread_ts>``.

The thread is the stable identity; Hermes' session id rotates on /new and compression.
Everything Vega knows about a thread lives in one JSON record under the runtime dir:
the messages people addressed to it (verbatim, for the topic's prompt), the topics and
tasks it opened, what has already been reported back, and whether a heartbeat is armed.

Nothing here imports Hermes. The gateway glue passes in the session's facts (channel,
thread, who is asking) and this module turns them into fleet commands through
:mod:`vega.fleet`, with the origin set by code and never by the model.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import fleet
from .config import VegaConfig
from .fleet import FleetError
from .runner import Runner

ORIGIN_PREFIX = "slack:"
_CHANNEL = re.compile(r"^[CGD][A-Z0-9]{2,}$")
_TS = re.compile(r"^\d{6,}\.\d{1,6}$")
_UNSAFE_NAME = re.compile(r"[\[\]\r\n\t]+")
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 8000
MAX_CONTEXT_CHARS = 20_000
CONTEXT_NOTE = (
    "Earlier messages in this Slack thread, for background. Anyone in the channel "
    "may have written them; only the request above is the ask."
)
BRIEF_SECTIONS = (
    ("what_to_do", "What to do"),
    ("hard_constraints", "Hard constraints"),
    ("coordination", "Coordination"),
    ("done_means", "Done means"),
)
TERMINAL_STATES = ("landed", "abandoned")
SEND_WINDOW_SECONDS = 3600
DISPATCH_TIMEOUT = 300


class OriginError(ValueError):
    """The thread cannot do what was asked: unknown ref, no messages, over a cap."""


@dataclass
class Thread:
    channel: str
    thread_ts: str
    messages: list[dict] = field(default_factory=list)
    context: str = ""
    topics: list[str] = field(default_factory=list)
    tasks: dict[str, dict] = field(default_factory=dict)
    sends: dict[str, list[float]] = field(default_factory=dict)
    heartbeat: dict = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return origin_of(self.channel, self.thread_ts)

    def open_refs(self) -> list[str]:
        return sorted(
            ref
            for ref, rec in self.tasks.items()
            if rec.get("state") not in TERMINAL_STATES
        )


# --- identity ------------------------------------------------------------------


def origin_of(channel: str, thread_ts: str) -> str:
    if not _CHANNEL.match(channel or "") or not _TS.match(thread_ts or ""):
        raise OriginError(f"not a Slack thread: {channel!r}/{thread_ts!r}")
    return f"{ORIGIN_PREFIX}{channel}/{thread_ts}"


def parse_origin(text: str) -> tuple[str, str]:
    """``slack:C…/1712345678.123456`` or ``C…/1712345678.123456`` -> (channel, ts)."""
    body = text.strip().removeprefix(ORIGIN_PREFIX)
    channel, sep, ts = body.partition("/")
    if not sep:
        raise OriginError(f"not a Slack thread: {text!r}")
    origin_of(channel, ts)
    return channel, ts


# --- the record ------------------------------------------------------------------


def thread_path(cfg: VegaConfig, channel: str, thread_ts: str) -> Path:
    origin_of(channel, thread_ts)
    return cfg.runtime_dir / "threads" / f"{channel}-{thread_ts}.json"


def load_thread(cfg: VegaConfig, channel: str, thread_ts: str) -> Thread:
    path = thread_path(cfg, channel, thread_ts)
    if not path.is_file():
        return Thread(channel=channel, thread_ts=thread_ts)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Thread(
        channel=channel,
        thread_ts=thread_ts,
        messages=list(data.get("messages") or []),
        context=str(data.get("context") or ""),
        topics=list(data.get("topics") or []),
        tasks=dict(data.get("tasks") or {}),
        sends={k: list(v) for k, v in (data.get("sends") or {}).items()},
        heartbeat=dict(data.get("heartbeat") or {}),
    )


def save_thread(cfg: VegaConfig, thread: Thread) -> Path:
    path = thread_path(cfg, thread.channel, thread.thread_ts)
    _write_private(path, json.dumps(asdict(thread), ensure_ascii=False, indent=1))
    return path


def list_threads(cfg: VegaConfig) -> list[Thread]:
    folder = cfg.runtime_dir / "threads"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        channel, _, ts = path.stem.partition("-")
        try:
            out.append(load_thread(cfg, channel, ts))
        except (OriginError, ValueError, OSError):
            continue
    return out


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def record_message(
    cfg: VegaConfig,
    *,
    channel: str,
    thread_ts: str,
    message_ts: str,
    user_id: str,
    display_name: str,
    text: str,
    context: str = "",
) -> Thread:
    """Keep what a person said to Vega in this thread, verbatim, for the prompt."""
    thread = load_thread(cfg, channel, thread_ts)
    body = text.strip()
    if len(body) > MAX_MESSAGE_CHARS:
        body = body[:MAX_MESSAGE_CHARS].rstrip() + " …"
    if body and not any(
        m.get("ts") == message_ts and m.get("text") == body for m in thread.messages
    ):
        thread.messages.append(
            {
                "ts": message_ts,
                "user": user_id,
                "name": clean_name(display_name) or user_id,
                "text": body,
            }
        )
        thread.messages = thread.messages[-MAX_MESSAGES:]
    if context and not thread.context:
        ctx = context.strip()
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = "… (earlier messages cut)\n" + ctx[-MAX_CONTEXT_CHARS:]
        thread.context = ctx
    save_thread(cfg, thread)
    return thread


def clean_name(name: str) -> str:
    return " ".join(_UNSAFE_NAME.sub(" ", name or "").split())


# --- intake: topic and tasks -------------------------------------------------------


def write_prompt(cfg: VegaConfig, thread: Thread, topic: str, title: str) -> Path:
    """The topic's PROMPT.md body: the thread's requests, verbatim, by name."""
    if not thread.messages:
        raise OriginError("no message from this thread to write the prompt from")
    parts = [
        f"# {title}",
        "",
        f"Requested in Slack ({thread.origin}).",
        "",
        "## Request",
        "",
    ]
    for m in thread.messages:
        parts += [f"[{m['name']}]", m["text"], ""]
    if thread.context:
        parts += ["## Thread so far", "", CONTEXT_NOTE, "", thread.context, ""]
    path = cfg.runtime_dir / "prompts" / f"{topic}.md"
    _write_private(path, "\n".join(parts))
    return path


def topic_add(
    cfg: VegaConfig,
    thread: Thread,
    *,
    slug: object,
    title: object,
    by: str,
    runner: Runner | None = None,
) -> dict:
    topic = fleet.check_slug(slug, "topic")
    name = fleet.check_title(title)
    prompt = write_prompt(cfg, thread, topic, name)
    args = [
        "queue",
        "topic",
        "add",
        topic,
        "--title",
        name,
        "--prompt-file",
        str(prompt),
    ]
    who = clean_name(by)
    if who:
        args += ["--by", who]
    result = fleet.run_fleet(cfg, args, runner=runner, timeout=60)
    if result.returncode != 0:
        raise FleetError(f"fleet queue topic add failed: {fleet.output_of(result)}")
    if topic not in thread.topics:
        thread.topics.append(topic)
    save_thread(cfg, thread)
    return {
        "topic": topic,
        "title": name,
        "prompt_file": str(prompt),
        "output": fleet.scrub(result.stdout),
    }


def write_brief(cfg: VegaConfig, topic: str, slug: str, sections: dict) -> Path:
    """The task's BRIEF.md body with fleet's four headings, or refuse."""
    parts = []
    for key, heading in BRIEF_SECTIONS:
        text = str(sections.get(key) or "").strip()
        if not text:
            raise OriginError(f"brief section {key!r} ({heading}) is empty")
        parts += [f"## {heading}", "", text, ""]
    body = "\n".join(parts)
    if len(body) > cfg.max_brief_chars:
        raise OriginError(
            f"brief is {len(body)} chars; the cap is {cfg.max_brief_chars}"
        )
    path = cfg.runtime_dir / "briefs" / f"{topic}-{slug}.md"
    _write_private(path, body)
    return path


def task_add(
    cfg: VegaConfig,
    thread: Thread,
    *,
    topic: object,
    slug: object,
    title: object,
    repo: object,
    branch: object,
    sections: dict,
    by: str,
    base: object = None,
    publish: object = None,
    runner: Runner | None = None,
) -> dict:
    """``fleet queue add`` with ``--origin`` fixed to this thread and no ``--reply``."""
    topic_slug = fleet.check_slug(topic, "topic")
    task_slug = fleet.check_slug(slug, "task slug")
    name = fleet.check_title(title)
    repo_path = fleet.check_repo(cfg, repo)
    branch_name = fleet.check_branch(branch)
    brief = write_brief(cfg, topic_slug, task_slug, sections)
    args = [
        "queue",
        "add",
        topic_slug,
        task_slug,
        "--title",
        name,
        "--repo",
        repo_path,
        "--branch",
        branch_name,
        "--brief-file",
        str(brief),
        "--origin",
        thread.origin,
    ]
    if base not in (None, ""):
        args += ["--base", fleet.check_branch(base, "base")]
    method = fleet.check_publish(publish)
    if method:
        args += ["--publish", method]
    who = clean_name(by)
    if who:
        args += ["--by", who]
    result = fleet.run_fleet(cfg, args, runner=runner, timeout=60)
    if result.returncode != 0:
        raise FleetError(f"fleet queue add failed: {fleet.output_of(result)}")
    ref = _ref_from_output(result.stdout, topic_slug)
    thread.tasks[ref] = {
        "state": "queued",
        "outcome": "",
        "artifacts": [],
        "needs_input": "",
    }
    if topic_slug not in thread.topics:
        thread.topics.append(topic_slug)
    save_thread(cfg, thread)
    return {"ref": ref, "brief_file": str(brief), "output": fleet.scrub(result.stdout)}


def _ref_from_output(stdout: str, topic: str) -> str:
    for line in reversed((stdout or "").splitlines()):
        for word in line.split():
            if word.startswith(f"{topic}/") and fleet.REF_RE.match(word):
                return word
    raise FleetError("fleet queue add printed no task ref")


# --- dispatch ---------------------------------------------------------------------


def dispatch(cfg: VegaConfig, thread: Thread, runner: Runner | None = None) -> dict:
    """Read fuel, then ``dispatch --origin <this thread>`` only when clearly above."""
    fuel = fleet.run_fleet(cfg, list(fleet.VIEWS["fuel"]), runner=runner, timeout=90)
    accounts = fleet.parse_fuel(fuel.stdout or "") if fuel.returncode == 0 else []
    ok, why = fleet.fuel_gate(accounts, cfg.fuel_providers, cfg.fuel_margin)
    if not ok:
        return {
            "dispatched": False,
            "reason": why,
            "left_for": "the conductor, which dispatches near the reserve",
        }
    result = fleet.run_fleet(
        cfg,
        ["queue", "dispatch", "--origin", thread.origin],
        runner=runner,
        timeout=DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        raise FleetError(f"fleet queue dispatch failed: {fleet.output_of(result)}")
    return {"dispatched": True, "reason": why, "output": fleet.scrub(result.stdout)}


# --- talking to a worker --------------------------------------------------------------


def live_tasks(
    cfg: VegaConfig, thread: Thread, runner: Runner | None = None
) -> list[dict]:
    rows = fleet.find_origin(cfg, thread.origin, runner)
    changed = False
    for row in rows:
        ref = row["ref"]
        if ref not in thread.tasks:
            thread.tasks[ref] = {
                "state": "",
                "outcome": "",
                "artifacts": [],
                "needs_input": "",
            }
            changed = True
    if changed:
        save_thread(cfg, thread)
    return rows


def send(
    cfg: VegaConfig,
    thread: Thread,
    *,
    ref: object,
    answer: object,
    runner: Runner | None = None,
) -> dict:
    """``fleet queue send`` to one of this thread's live workers, rate-capped."""
    target = fleet.check_ref(ref)
    line = fleet.check_line(answer, "answer")
    live = {row["ref"] for row in fleet.find_origin(cfg, thread.origin, runner)}
    if target not in live:
        raise OriginError(f"{target} is not a live task of this thread")
    now = time.time()
    recent = [t for t in thread.sends.get(target, []) if now - t < SEND_WINDOW_SECONDS]
    if len(recent) >= cfg.max_sends_per_hour:
        raise OriginError(
            f"{target} was already sent {len(recent)} messages this hour; wait"
        )
    result = fleet.run_fleet(
        cfg, ["queue", "send", target, line], runner=runner, timeout=60
    )
    thread.sends[target] = [*recent, now]
    save_thread(cfg, thread)
    if result.returncode != 0:
        raise FleetError(f"fleet queue send failed: {fleet.output_of(result)}")
    return {"ref": target, "sent": line, "output": fleet.scrub(result.stdout)}


# --- news: what changed since the thread was last told -----------------------------


def news(cfg: VegaConfig, thread: Thread, runner: Runner | None = None) -> dict:
    """Diff fleet's records for this thread against what was already reported.

    Returns ``{"news": [...], "open": [...], "done": bool}``. ``done`` is True when no
    task of the thread is still open, which is when the heartbeat may be cleared.
    """
    live_tasks(cfg, thread, runner)
    events = []
    for ref in sorted(thread.tasks):
        prev = thread.tasks[ref]
        if prev.get("state") in TERMINAL_STATES:
            continue
        record = fleet.show(cfg, ref, runner)
        events += _diff(ref, prev, record)
        thread.tasks[ref] = {
            "state": str(record.get("state") or ""),
            "outcome": str(record.get("outcome") or ""),
            "artifacts": fleet.artifact_urls(record),
            "needs_input": _latest_need(record, prev.get("needs_input") or "")[0],
            "title": str(record.get("title") or prev.get("title") or ""),
        }
    save_thread(cfg, thread)
    open_refs = thread.open_refs()
    return {"news": events, "open": open_refs, "done": not open_refs}


def _diff(ref: str, prev: dict, record: dict) -> list[dict]:
    events: list[dict] = []
    state = str(record.get("state") or "")
    was = str(prev.get("state") or "")
    title = str(record.get("title") or "")
    urls = fleet.artifact_urls(record)
    new_urls = [u for u in urls if u not in (prev.get("artifacts") or [])]

    def add(kind: str, **extra: object) -> None:
        events.append({"ref": ref, "kind": kind, "title": title, **extra})

    if state != was:
        if state == "dispatched":
            add("started")
        elif state == "landed":
            add("landed", urls=urls)
        elif state == "abandoned":
            add("abandoned", why=str(record.get("abandoned") or ""))
        elif state in ("stuck", "failed"):
            add(state, outcome=str(record.get("outcome") or ""))
    outcome = str(record.get("outcome") or "")
    if outcome == "shipped" and prev.get("outcome") != "shipped":
        add("shipped", urls=urls)
    elif new_urls and state not in TERMINAL_STATES and outcome != "shipped":
        add("pull_request", urls=new_urls)
    observed, why = _latest_need(record, str(prev.get("needs_input") or ""))
    if why:
        add("needs_input", question=why, observed=observed)
    return events


def _latest_need(record: dict, after: str) -> tuple[str, str]:
    """(observed, why) of the newest needs-input declaration newer than ``after``."""
    path = fleet.progress_path(record)
    if path is None or not path.is_file():
        return after, ""
    latest = (after, "")
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return after, ""
    for row in rows:
        try:
            doc = json.loads(row)
        except ValueError:
            continue
        said = doc.get("declare") if isinstance(doc, dict) else None
        if not isinstance(said, dict) or said.get("state") != "needs-input":
            continue
        observed = str(doc.get("observed") or "")
        if observed and observed > latest[0]:
            latest = (observed, str(said.get("why") or ""))
    return latest


def render_news(events: list[dict]) -> list[str]:
    """One plain line per event, the way it should read in Slack."""
    lines = []
    for e in events:
        ref, title = e["ref"], e.get("title") or ""
        label = f"{ref}" + (f" ({title})" if title else "")
        kind = e["kind"]
        if kind == "started":
            lines.append(f"{label}: a worker started on it.")
        elif kind == "shipped":
            urls = " ".join(e.get("urls") or [])
            lines.append(f"{label}: shipped{' - ' + urls if urls else ''}.")
        elif kind == "pull_request":
            lines.append(f"{label}: pull request opened - {' '.join(e['urls'])}.")
        elif kind == "landed":
            lines.append(f"{label}: landed (merged).")
        elif kind == "abandoned":
            why = e.get("why") or "no reason recorded"
            lines.append(f"{label}: abandoned - {why}.")
        elif kind == "needs_input":
            lines.append(f"{label}: the worker needs input - {e.get('question')}")
        else:
            lines.append(
                f"{label}: {kind}{' - ' + e['outcome'] if e.get('outcome') else ''}."
            )
    return lines
