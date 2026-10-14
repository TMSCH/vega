"""Vega: the Slack front door to fleet. Each Slack thread is its own fleet origin.

This directory is both the Hermes plugin (``register`` below, loaded from Vega's
profile) and the ``vega`` Python package behind the ``vega`` command. Nothing here
imports Hermes at module level, so the command runs without it.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.2.0"


def register(ctx: Any) -> None:
    from .hermes_glue import register as _register

    _register(ctx)
