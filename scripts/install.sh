#!/usr/bin/env bash
# Install or update Vega on this machine:
#   1. the Hermes profile `vega` (SOUL.md, config.yaml, skills, the vega plugin),
#   2. vega.toml in that profile from vega.example.toml, if there is none yet,
#   3. the `vega` command (for `vega reply`, `vega admit`, `vega status`, `vega news`).
# It never touches any other Hermes profile and never writes a token.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile="${VEGA_PROFILE:-vega}"

command -v hermes >/dev/null || {
  echo "hermes is not installed: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash" >&2
  exit 1
}
command -v uv >/dev/null || { echo "uv is not installed: https://docs.astral.sh/uv/" >&2; exit 1; }

# Hermes refuses a distribution that contains symlinks (a local .venv has them),
# so the profile is installed from a clean export of the committed files. The
# export lives at a fixed path because `hermes profile update` re-reads it.
stage="${VEGA_DIST_DIR:-$HOME/.local/share/vega/dist}"
rm -rf "$stage" && mkdir -p "$stage"
git -C "$repo" archive HEAD | tar -x -C "$stage"

if hermes profile list 2>/dev/null | grep -qw "$profile"; then
  hermes profile update "$profile" --yes   # keeps config.yaml, .env and vega.toml
else
  hermes profile install "$stage" --name "$profile" --alias --yes
fi

home="${HERMES_ROOT:-$HOME/.hermes}/profiles/$profile"
if [[ ! -f "$home/vega.toml" ]]; then
  install -m 600 "$repo/vega.example.toml" "$home/vega.toml"
  echo "wrote $home/vega.toml: add your Slack user and channel ids to [access]"
fi

uv tool install --force --editable "$repo"
echo "installed: $(command -v vega || echo 'vega (add ~/.local/bin to PATH)')"
