#!/usr/bin/env bash
# Create Vega's lead session in thurbox under a stable name, so fleet can find it.
#
# fleet sends ready / stalled / needs-input notices for `slack:`-origin tasks to a
# session whose name contains a configured substring. The operator adds this line
# to fleet's orchestration/owners.conf (gitignored, theirs to edit):
#
#     slack: = Vega Slack Lead
#
# The name below contains that substring. Override with VEGA_LEAD_NAME and keep the
# owners.conf line in step. thurbox matches names, so the glyph may differ.
set -euo pipefail
name="${VEGA_LEAD_NAME:-🛰 Vega Slack Lead}"
profile="${VEGA_PROFILE:-vega}"
fleet="${VEGA_FLEET_PROJECT:-$HOME/fleet}"

# The lead runs in the fleet checkout, so `fleet queue` resolves its queue. It needs
# a terminal, which the Slack gateway's toolset deliberately lacks.
exec thurbox-cli session create --name "$name" --repo-path "$fleet" \
  --command "hermes -p $profile -t vega,terminal chat"
