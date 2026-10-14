"""Every ``fleet`` command Vega runs, and the checks on what the model may put in one.

Only argv built here is ever run: ``uv run --project <fleet.project> fleet …`` with no
shell, a timeout, and an environment stripped of Slack tokens and of the thurbox
markers that would make ``fleet queue add`` record the gateway's launcher as the task's
origin. Free text from the model reaches an argv only as a validated ref, slug, branch,
title or answer, never as a flag: anything starting with ``-`` is refused.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import runner as _runner
from .config import VegaConfig
from .runner import Completed, Runner

VIEWS: dict[str, tuple[str, ...]] = {
    "status": ("status",),
    "fuel": ("status", "--fuel"),
    "queue": ("queue", "list", "--live"),
    "plan": ("queue", "plan"),
    "context": ("context",),
}
VIEW_TIMEOUT = 90
MAX_OUTPUT_CHARS = 3500
# What a fleet child must not inherit: Vega's Slack tokens, and thurbox's session
# markers, which `fleet queue add` would otherwise record as the origin.
DROP_ENV = ("SLACK_*", "THURBOX_SESSION", "THURBOX_TASK")
PUBLISH_METHODS = ("pr", "attested", "push", "note", "served", "none")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
REF_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}/\d{2}-[a-z0-9][a-z0-9-]{0,60}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
_SHOW_LINE = re.compile(r"^ {4}([a-z_]+):\s*(.*)$")
MAX_TITLE_CHARS = 120


class FleetError(ValueError):
    """A value the model supplied is unusable, or fleet refused the command."""


def argv(view: str, project: Path) -> list[str]:
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; choose one of {', '.join(VIEWS)}")
    return fleet_argv(project, *VIEWS[view])


def fleet_argv(project: Path, *args: str) -> list[str]:
    return ["uv", "run", "--project", str(project), "fleet", *args]


def run_fleet(
    cfg: VegaConfig,
    args: list[str],
    *,
    runner: Runner | None = None,
    timeout: float = 60,
) -> Completed:
    """Run one fleet command with the minimal environment and a timeout."""
    for arg in args:
        if not isinstance(arg, str):
            raise FleetError(f"argv must be strings, got {type(arg).__name__}")
    return (runner or _runner.run)(
        fleet_argv(cfg.fleet_project, *args), timeout=timeout, drop_env=DROP_ENV
    )


def read_view(view: str, cfg: VegaConfig, runner: Runner | None = None) -> str:
    """Run one read-only fleet command and return its output, trimmed for Slack."""
    result = run_fleet(cfg, list(VIEWS[view]), runner=runner, timeout=VIEW_TIMEOUT)
    return scrub(output_of(result))


def output_of(result: Completed) -> str:
    out = _ANSI.sub("", result.stdout or "")
    if result.returncode != 0:
        err = _ANSI.sub("", result.stderr or "").strip()
        out = f"{out}\n(fleet exited {result.returncode}: {err})".strip()
    return out


def scrub(text: str, home: Path | None = None) -> str:
    """Shorten the home directory to ``~`` and cap the length."""
    home_str = str(home or Path.home())
    text = text.replace(home_str, "~").strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS].rstrip() + "\n… (truncated)"
    return text


# --- values the model supplies ---------------------------------------------------


def check_slug(value: object, what: str = "slug") -> str:
    text = str(value or "").strip()
    if not SLUG_RE.match(text):
        raise FleetError(
            f"{what} {text!r} must be lowercase letters, digits and dashes"
        )
    return text


def check_ref(value: object) -> str:
    text = str(value or "").strip()
    if not REF_RE.match(text):
        raise FleetError(f"ref {text!r} must look like <topic>/<NN>-<task>")
    return text


def check_branch(value: object, what: str = "branch") -> str:
    text = str(value or "").strip()
    if not BRANCH_RE.match(text) or ".." in text or text.endswith((".lock", "/")):
        raise FleetError(f"{what} {text!r} is not a usable git branch name")
    return text


def check_title(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text or text.startswith("-"):
        raise FleetError("title must be a non-empty line that does not start with -")
    if len(text) > MAX_TITLE_CHARS:
        raise FleetError(f"title must be at most {MAX_TITLE_CHARS} characters")
    return text


def check_publish(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text not in PUBLISH_METHODS:
        raise FleetError(f"publish must be one of {', '.join(PUBLISH_METHODS)}")
    return text


def check_repo(cfg: VegaConfig, value: object) -> str:
    """A repository path, only if ``fleet.repos`` in ``vega.toml`` lists it."""
    text = str(value or "").strip()
    if not text or text.startswith("-"):
        raise FleetError("repo must be a path from the allowlist")
    if not cfg.repos:
        raise FleetError(
            "no repositories are allowlisted (fleet.repos in vega.toml), so no task "
            "can be added"
        )
    wanted = Path(text).expanduser()
    for allowed in cfg.repos:
        if _same(wanted, allowed):
            return str(allowed)
    names = ", ".join(sorted(p.name for p in cfg.repos))
    raise FleetError(f"repo {text!r} is not allowlisted; allowed: {names}")


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.name == b.name and str(a) == a.name)
    except OSError:
        return False


def check_line(value: object, what: str, limit: int = 2000) -> str:
    """One line of free text that may follow a ref on an argv."""
    text = " ".join(str(value or "").split())
    if not text or text.startswith("-"):
        raise FleetError(f"{what} must be a non-empty line that does not start with -")
    if len(text) > limit:
        raise FleetError(f"{what} must be at most {limit} characters")
    return text


# --- fuel ---------------------------------------------------------------------


@dataclass(frozen=True)
class FuelAccount:
    provider: str
    remaining: int | None
    reserve: int
    state: str
    stale: bool

    def clearly_above(self, margin: int) -> bool:
        return (
            self.state == "fresh"
            and not self.stale
            and self.remaining is not None
            and self.remaining >= self.reserve + margin
        )


def parse_fuel(text: str) -> list[FuelAccount]:
    """``fleet status --fuel``: blank-line separated ``name<TAB>value`` records."""
    accounts = []
    for block in re.split(r"\n\s*\n", text.strip()):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            name, sep, value = line.partition("\t")
            if sep and name not in fields:
                fields[name] = value.strip()
        if not fields:
            continue
        accounts.append(
            FuelAccount(
                provider=fields.get("provider", "?"),
                remaining=_maybe_int(fields.get("remaining")),
                reserve=_maybe_int(fields.get("reserve")) or 0,
                state=fields.get("state", "unavailable"),
                stale=fields.get("stale", "0") not in ("0", "", "false"),
            )
        )
    return accounts


def _maybe_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def fuel_gate(
    accounts: list[FuelAccount], providers: tuple[str, ...], margin: int
) -> tuple[bool, str]:
    """fleet-intake's rule: dispatch only clearly above the reserve on every account.

    ``providers`` names the accounts to read; empty means the first one printed,
    which is the checkout's own. A missing, stale or unavailable reading is a no.
    """
    if not accounts:
        return False, "fuel could not be read"
    if providers:
        by_name = {a.provider: a for a in accounts}
        chosen = [
            by_name.get(p) or FuelAccount(p, None, 0, "missing", True)
            for p in providers
        ]
    else:
        chosen = accounts[:1]
    short = [a for a in chosen if not a.clearly_above(margin)]
    if short:
        words = ", ".join(
            f"{a.provider} {a.remaining if a.remaining is not None else '?'}% "
            f"({a.state}{', stale' if a.stale else ''}; floor {a.reserve + margin}%)"
            for a in short
        )
        return False, f"fuel not clearly above the reserve: {words}"
    words = ", ".join(f"{a.provider} {a.remaining}%" for a in chosen)
    return True, f"fuel clearly above the reserve: {words}"


# --- records -------------------------------------------------------------------


def find_origin(
    cfg: VegaConfig, origin: str, runner: Runner | None = None
) -> list[dict]:
    """``fleet queue find --origin … --json``: this origin's live tasks."""
    result = run_fleet(
        cfg, ["queue", "find", "--origin", origin, "--json"], runner=runner, timeout=60
    )
    if result.returncode != 0:
        raise FleetError(f"fleet queue find failed: {output_of(result)}")
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise FleetError(f"fleet queue find returned non-JSON: {exc}") from exc
    return [
        r for r in rows if isinstance(r, dict) and REF_RE.match(str(r.get("ref", "")))
    ]


def show(cfg: VegaConfig, ref: str, runner: Runner | None = None) -> dict:
    """``fleet queue show <ref>`` parsed into a dict; repeated keys become lists."""
    result = run_fleet(cfg, ["queue", "show", ref], runner=runner, timeout=60)
    if result.returncode != 0:
        raise FleetError(f"fleet queue show failed: {output_of(result)}")
    return parse_show(_ANSI.sub("", result.stdout or ""))


def parse_show(text: str) -> dict:
    record: dict = {"raw": text}
    lines = text.splitlines()
    if lines:
        head, _, title = lines[0].partition(" — ")
        record["ref"], record["title"] = head.strip(), title.strip()
    for line in lines[1:]:
        match = _SHOW_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value == "None":
            value = ""
        if key in record and key != "raw":
            prior = record[key]
            record[key] = (prior if isinstance(prior, list) else [prior]) + [value]
        else:
            record[key] = value
    return record


def artifact_urls(record: dict) -> list[str]:
    value = record.get("artifact") or []
    values = value if isinstance(value, list) else [value]
    urls = []
    for v in values:
        for word in str(v).split():
            if word.startswith(("http://", "https://")):
                urls.append(word)
    return urls


def progress_path(record: dict) -> Path | None:
    """The task's progress.jsonl, beside the brief ``show`` names."""
    brief = record.get("brief")
    if not brief or not isinstance(brief, str):
        return None
    return Path(brief).parent / "progress.jsonl"
