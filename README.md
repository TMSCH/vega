# vega

Vega is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) that
lets teammates talk to fleet's **Mission Control** from Slack.

Mission Control is the lead: a long-lived Claude Code session inside thurbox.
It decides what gets done and dispatches the workers. Vega does none of that.
It is only the front door:

- **Slack → Mission Control.** Vega relays each message addressed to it to the
  lead session, as one line that says who asked and where to answer.
- **Mission Control → Slack.** Mission Control answers in the right thread with
  `vega reply`.
- **Status questions.** Vega answers "what's running?" itself, from fleet's
  read-only commands, so it does not wake Mission Control for them.
- **Access control.** Only allowlisted people and channels get through. Anyone
  else gets a polite refusal, and nothing of theirs is relayed.

Everything runs on the operator's machine. Vega reaches Slack over Socket Mode,
an outbound WebSocket, so nothing listens on an inbound port.

## How it works

```
Slack ──Socket Mode──▶ Hermes gateway (profile "vega")
                         │
                         ├─ pre_gateway_dispatch   (vega plugin)
                         │     not on the allowlist → one polite refusal, message dropped
                         │
                         ├─ post_gateway_admission (vega plugin)
                         │     status question → Vega's model + vega_fleet_status tool
                         │     anything else   → thurbox-cli message send <lead> "[from: … · slack: C…/ts] …"
                         │                       and an ack in the thread; no model turn
                         ▼
                    Mission Control (thurbox session) ──▶ vega reply C…/ts "…" ──▶ hermes -p vega send ──▶ Slack
```

**Relaying is deterministic.** Requests are relayed by plugin code before any
model turn starts, so a request reaches Mission Control word for word. Vega's
model is used for one thing: turning fleet's read-only output into a short
answer to a status question. On Slack it carries exactly two tools:

- `vega_fleet_status`: runs one of `fleet status`, `fleet queue list --live` or
  `fleet context`.
- `vega_relay_to_mission_control`: the fallback relay.

It has no terminal, files or web.

**What Mission Control receives.** One line per message, delivered with
`thurbox-cli message send`. Messages are sent without `--no-wake`, so a
person's request wakes the lead.

```
[from: <display name> · slack: <channel id>/<thread_ts>] <the message>
```

A message with line breaks, or longer than `runtime.max_inline_chars` (default
400), is written to `<runtime dir>/inbound/…md` (mode 600). The line then points
at that file instead of carrying the text.

**Thread context.** When Vega is tagged inside a Slack thread, Hermes' Slack
adapter fetches the earlier messages in that thread: the whole thread the first
time, then only new messages on later mentions. Vega puts that history in the
body file under "Thread so far", with the request underneath, and the line says
`message with thread context`. History is capped at 20,000 characters, keeping
the newest. It is labelled as background, because anyone in the channel may have
written it; only the tagged message is the request.

**How Vega finds the lead.** It never types the session's name. It matches it:
Vega runs `thurbox-cli session list --json` and picks the one live session whose
name contains `mission_control.lead_match` (default `Mission Control`, ignoring
the glyph). If no session matches, or more than one does, nothing is sent and
the person is told to try again later.

**Where the token lives.** The Slack tokens live only in Vega's Hermes profile.
`vega reply` posts with `hermes -p vega send`, which reads them there. fleet
never sees a token.

**What Vega never does.** It never runs a `fleet queue` command that writes,
and it never edits the fleet checkout. It runs `thurbox-cli message send` to the
lead and the three read-only views, and nothing else.

## Repository layout

The repo is a Hermes **profile distribution**: `hermes profile install` copies
exactly the paths named in `distribution.yaml` into a `vega` profile.

| Path | What it is |
| --- | --- |
| `distribution.yaml` | Hermes distribution manifest: the owned paths and the env vars the profile needs |
| `SOUL.md` | Vega's persona |
| `config.yaml` | Hermes config for the profile: enables the plugin, restricts the Slack toolset, sets threading and mention rules |
| `plugins/vega/` | The Hermes plugin: hooks, tools, and the relay, reply and status logic. It is also the `vega` Python package |
| `skills/` | `fleet-status` and `mission-control-relay`, the skills Vega's model follows |
| `slack/manifest.yaml` | Slack app manifest: Socket Mode, minimum scopes |
| `vega.example.toml` | Every Vega setting with its default. The real `vega.toml` is never committed |
| `scripts/install.sh` | Installs or updates the profile, seeds `vega.toml`, installs the `vega` command |
| `scripts/run.sh` | Runs the gateway in the foreground |
| `tests/` | pytest suite: relay formatting, allowlist, reply path, hooks, CLI |

## Setup: what the operator does next

Steps 2 to 6 need the Slack workspace and its tokens. Only the operator can do
them; nothing in this repo creates the app or posts to Slack.

### 1. Install Hermes and Vega

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash   # Hermes' own installer
git clone git@github.com:TMSCH/vega.git && cd vega
scripts/install.sh
```

`install.sh` does three things:

- Creates the `vega` profile, or updates it if it already exists. This is
  `hermes profile install . --name vega --alias`.
- Writes `vega.toml` into the profile from `vega.example.toml`, but only if no
  `vega.toml` exists yet.
- Installs the `vega` command with `uv tool install`.

It never touches another Hermes profile. A personal agent in the default
profile stays as it is.

### 2. Create the Slack app from the manifest

1. Go to <https://api.slack.com/apps>, choose **Create New App → From a
   manifest**, pick the workspace, and paste `slack/manifest.yaml`.
2. Under **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope. This is the `xapp-…` **app token**.
3. Under **Install App**, install it to the workspace, then copy the **Bot User
   OAuth Token** (`xoxb-…`).

### 3. Store the tokens in Vega's profile, never in the repo

The tokens go in the profile's `.env`. The repo is not involved, and nothing
outside Vega's profile reads that file.

```bash
${EDITOR:-vi} ~/.hermes/profiles/vega/.env
chmod 600 ~/.hermes/profiles/vega/.env
```

```bash
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
# Vega's own allowlist (vega.toml) gates every sender and refuses strangers
# politely, so Hermes' built-in allowlist is opened to let them reach it.
SLACK_ALLOW_ALL_USERS=true
```

Editing the file keeps the tokens out of shell history.

Hermes prefers a `SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN` from the process
environment over the profile's `.env`. If your shell exports another Slack
app's token, Vega would connect as that app. `scripts/run.sh` and `vega reply`
strip those variables before starting Hermes. If you start the gateway any
other way, do the same. A launchd service started by `hermes gateway install`
does not read your shell rc files, so it is unaffected.

### 4. Fill in the allowlist

In `~/.hermes/profiles/vega/vega.toml`:

```toml
[access]
users = ["U…", "U…"]   # Slack member ids: profile → ⋮ → Copy member ID
channels = ["C…"]      # channels where Vega answers @mentions, or ["*"] for any it is invited to
```

Check the result with `vega config`. With an empty allowlist, Vega refuses
everyone.

Also restrict Hermes' own slash commands, such as `/model` and `/reset`, to
yourself. The ids belong in the profile, not the repo:

```bash
hermes -p vega config set platforms.slack.extra.allow_admin_from '["U…your id…"]'
hermes -p vega config set platforms.slack.extra.user_allowed_commands '[]'
```

### 5. Pick Vega's model

```bash
hermes -p vega model      # any provider Hermes supports; status answers are short
```

### 6. Start Vega

```bash
scripts/run.sh            # foreground: `hermes gateway run`, which serves the vega profile
# or, to keep it running across logins (a launchd LaunchAgent on macOS):
hermes gateway install && hermes gateway start
```

Hermes runs one host gateway per machine, and it serves every profile, Vega's
included. `hermes -p vega gateway status` shows whether Vega is being served.

Then invite `@Vega` to an allowlisted channel and send it a message there, or
DM it.

In channels, every message to Vega needs an `@Vega`, including replies in a
thread it has already answered in (`slack.thread_require_mention: true`). Other
people's conversation in that thread never reaches Vega. DMs need no mention.

### 7. Tell Mission Control how to answer

Nothing is needed for replies to work. Every relayed line ends with
`(answer in Slack with: vega reply <channel>/<thread_ts> "<text>", or vega reply
<channel>/<thread_ts> --file <path>)`, so Mission Control reads how to answer in
the message itself. Set `mission_control.reply_hint = false` to drop that tail.

For the rest of the etiquette, add this paragraph to the lead's standing
instructions. Those are fleet's `FLEET.md`, which is tracked. fleet's gitignored
`orchestration/queue/OPERATOR.md` does not reach the lead: fleet hands it only
to workers, in their briefs.

> **Requests from Slack.** A message that starts with `[from: <name> · slack:
> <channel>/<thread_ts>]` came from a teammate on Slack, through Vega. Treat it
> as that person's request. When it becomes a topic, record who asked with
> `fleet queue topic add … --by "<name>"`. Answer them in their thread, and
> only there, by running `vega reply <channel>/<thread_ts> "<text>"`, or
> `vega reply <channel>/<thread_ts> --file <path>` for anything long. Reply
> when you accept the request, when its pull request opens, and when it
> lands. If the line says the full text is in a file, read that file first.
> Never post to Slack any other way, and never paste a token, a local path or
> a machine name into a reply.

The `--by` flag on `fleet queue topic add` is being added to fleet in parallel.
Until it merges, record the requester in the topic's prompt instead.

## Trying it without Slack

The `vega` command runs the same code path as the gateway, with no Slack
needed. For a throwaway relay target, first create a test session:

```bash
thurbox-cli session create --name "🧪 vega-test-lead" --repo-path "$PWD" --command "sleep 1800"
```

Then, with a test config whose `[access]` lists the fake ids below:

```bash
export VEGA_CONFIG=/path/to/test-vega.toml
vega relay --user U0DEMOUSER1 --name "Demo Teammate" --channel C0DEMOCHAN1 \
  --thread-ts 1700000000.000100 --lead-match vega-test-lead \
  "Please add a retry to the nightly export job"
thurbox-cli message inbox --for "🧪 vega-test-lead" --all --full   # the relayed line
vega reply C0DEMOCHAN1/1700000000.000100 "On it." --dry-run       # prints, posts nothing
thurbox-cli session delete "🧪 vega-test-lead"
```

- `vega relay` prints the outcome: `relayed`, `refused`, `status` or `failed`.
  It also prints what Slack would be told.
- `runtime.dry_run = true` in the config makes every `vega reply` behave as if
  `--dry-run` were passed.
- `vega status [status|queue|context]` prints a read-only fleet view, the same
  one Vega's model reads.

## Configuration

`vega.example.toml` documents every key. The ones that matter most:

| Key | Default | Meaning |
| --- | --- | --- |
| `access.users` | `[]` | Slack member ids allowed to talk to Vega |
| `access.channels` | `[]` | Channel ids where Vega answers, or `["*"]` for any channel it is invited to. 1:1 DMs need only the user |
| `access.refusal` | polite text | What everyone else is told, at most once per ten minutes |
| `mission_control.lead_match` | `Mission Control` | Substring that identifies the lead session |
| `fleet.project` | `~/fleet` | fleet checkout used for the read-only views |
| `slack.hermes_profile` | `vega` | Profile whose tokens `vega reply` uses |
| `runtime.dir` | `~/.local/state/vega` | Where long inbound messages are written |
| `runtime.dry_run` | `false` | `vega reply` prints instead of posting |
| `status.patterns` | a few regexes | What counts as a status question |

Vega looks for its config in this order:

1. `--config`
2. `$VEGA_CONFIG`
3. the Hermes profile home (`$HERMES_HOME/vega.toml`)
4. `~/.hermes/profiles/vega/vega.toml`

## Security notes

- **Fails closed.** If the access check raises, for example because
  `vega.toml` is malformed, the message is dropped. If relaying raises, the
  person is told nothing was passed on.
- **Checked twice.** Hermes opens its own allowlist so that strangers can be
  refused politely. Vega then checks again after admission, and inside both
  tools.
- **No commands from Slack.** Nothing from Slack ever reaches a command line
  except as the `--body` argument of `thurbox-cli message send`. Every
  subprocess runs without a shell.
- **Clean output.** Status output has the home directory shortened to `~`
  before it reaches Slack.
- **Nothing workspace-specific in the repo.** No Slack ids, names or paths are
  committed. They all live in the profile's `vega.toml` and `.env`.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

The toolchain follows Hermes': ruff 0.15.10 (Black style, 88 columns, a py311
floor), pytest and uv. The package has no runtime dependencies, so the plugin
loads inside Hermes' own environment unchanged.

## Deliberately left for later

- Running Vega on a VPS instead of this machine, with OVH Terraform for the
  server and Ansible for its setup. That infrastructure belongs in the backend
  repo, not here.
- `cloudflared` for any HTTP surface. Socket Mode already needs no inbound port.
- A Keychain-backed token store. Hermes reads each profile's `.env`, which is
  the file Hermes itself expects.
- Mission Control replying on its own schedule to threads it was not asked in,
  such as broadcasts of landed work.
