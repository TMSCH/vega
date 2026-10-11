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


def run(
    argv: Sequence[str],
    *,
    input: str | None = None,
    timeout: float = 30.0,
    drop_env: Sequence[str] = (),
) -> Completed:
    """Run ``argv`` (never through a shell) and capture its output.

    ``drop_env`` names variables the child must not inherit.
    """
    env = None
    if drop_env:
        env = {k: v for k, v in os.environ.items() if k not in set(drop_env)}
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            input=input,
            env=env,
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
