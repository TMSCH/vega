"""The one place Vega starts a subprocess, so tests can swap it for a fake."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., Completed]


def child_env(drop_env: Sequence[str]) -> dict[str, str] | None:
    """The environment a child gets: the parent's minus ``drop_env``.

    A name ending in ``*`` drops every variable with that prefix (``SLACK_*``). ``None``
    means the parent's environment unchanged.
    """
    if not drop_env:
        return None
    names = {d for d in drop_env if not d.endswith("*")}
    prefixes = tuple(d[:-1] for d in drop_env if d.endswith("*"))
    return {
        k: v
        for k, v in os.environ.items()
        if k not in names and not (prefixes and k.startswith(prefixes))
    }


def run(
    argv: Sequence[str],
    *,
    input: str | None = None,
    timeout: float = 30.0,
    drop_env: Sequence[str] = (),
) -> Completed:
    """Run ``argv`` (never through a shell) and capture its output.

    ``drop_env`` names variables the child must not inherit; see :func:`child_env`.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            input=input,
            env=child_env(drop_env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return Completed(127, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"{argv[0]}: timed out after {timeout:.0f}s")
    return Completed(proc.returncode, proc.stdout, proc.stderr)
