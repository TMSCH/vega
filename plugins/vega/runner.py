"""The one place Vega starts a subprocess, so tests can swap it for a fake."""

from __future__ import annotations

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
    argv: Sequence[str], *, input: str | None = None, timeout: float = 30.0
) -> Completed:
    """Run ``argv`` (never through a shell) and capture its output."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            input=input,
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
