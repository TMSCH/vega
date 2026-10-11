#!/usr/bin/env bash
# Run Vega in the foreground on this machine (Ctrl-C stops it).
#
# Hermes serves every profile from one host gateway, so this starts the host
# gateway, which serves the `vega` profile. If a host gateway is already running
# (e.g. installed with `hermes gateway install`), there is nothing to start:
# `hermes -p vega gateway status` shows whether it serves Vega.
set -euo pipefail
profile="${VEGA_PROFILE:-vega}"

vega config
hermes -p "$profile" gateway status || true
exec hermes gateway run "$@"
