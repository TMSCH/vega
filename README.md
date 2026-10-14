# vega

Vega is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) that
lets teammates hand work to fleet from Slack. **Each Slack thread is its own
fleet origin**: the Hermes conversation behind the thread takes the request,
opens the topic and its tasks, starts them when fuel allows, and reports back in
the same thread as the work moves. No lead session sits in between.

- **Intake from the thread.** Vega's model runs fleet's intake with narrow
  plugin tools: `vega_topic_add`, `vega_task_add`, `vega_dispatch`. Every fleet
  command is a fixed argv built in code; the origin `slack:<channel>/<thread_ts>`
  is set by the plugin, never by the model.
- **News by polling, not by push.** Once a thread has tasks, the conversation
  gets a Hermes heartbeat. Each tick calls `vega_fleet_news`, which reads
  fleet's own records (`find --origin`, `show`, the task's `progress.jsonl`),
  diffs them against what the thread was already told, and returns only what
  is new: started, pull request opened, shipped with the link, landed,
  abandoned and why, a worker's question. With nothing new the turn ends with
  `NO_REPLY`, which posts nothing. When every task has landed or been
  abandoned, the heartbeat is cleared.
- **Status questions.** `vega_fleet_status`, `vega_find` and `vega_show` read
  fleet's views.
- **Access control.** Only allowlisted people and channels get through. Anyone
  else gets a polite refusal, and nothing of theirs is recorded.

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
                         │     records the message verbatim in the thread's record,
                         │     lets the model turn run (a heartbeat tick is recognised
                         │     and not recorded)
                         ▼
                    Vega's model + tools, for this thread only
                         │  vega_topic_add   → fleet queue topic add … --by <asker>
                         │  vega_task_add    → fleet queue add … --origin slack:C…/ts
                         │  vega_dispatch    → fleet status --fuel, then
                         │                    fleet queue dispatch --origin slack:C…/ts
                         │  vega_fleet_news  → find --origin, show, progress.jsonl → diff
                         │  vega_send        → fleet queue send <ref> '<answer>'
                         ▼
                    Hermes heartbeat (every 5 min while the thread has open tasks)
                         → a normal idle-only turn → vega_fleet_news → post or NO_REPLY
```

**The origin is the thread, not the session.** Hermes' session id rotates on
`/new`, `/stop` and context compression; the Slack thread does not. So the
task records carry `slack:<channel>/<thread_ts>`, and Vega keeps its own record
per thread under the runtime dir: the messages people addressed to it (verbatim,
for the topic prompt), the topics and refs it opened, what has been reported,
and the session the heartbeat was armed on.

**How the heartbeat starts.** Hermes registers a heartbeat watch from the
`/heartbeat` command and from a restore scan. That scan is not only for
restarts: the gateway's heartbeat poller runs it every few seconds over each
served profile's session store. So the plugin only has to write the session's
heartbeat row through Hermes' public `HeartbeatManager`, and the poller picks it
up. The session id is read from the gateway's session store by the session key,
which is bound on every turn, with `HERMES_SESSION_ID` as the fallback. All of
that sits in `plugins/vega/heartbeat.py`.

**What a tick looks like.** The gateway injects the heartbeat prompt as an
ordinary user turn, only when the session is idle, and never interrupts a
running turn. Vega's hooks see it like any message: the allowlist check passes
(the stored source is the person who started the thread), and the admission
hook recognises the `[Heartbeat` prefix and records nothing. The model calls
`vega_fleet_news` and posts the lines it returns, or `NO_REPLY`.

**The fuel gate is code.** `vega_dispatch` reads `fleet status --fuel` and
dispatches only when every gated account is `fresh`, not `stale`, and at least
`reserve + fleet.fuel_margin` (10 by default). Otherwise the tasks stay queued
for the conductor, which dispatches near the reserve with one reading, and the
tool says so.

**What the model can and cannot put on a command line.** Refs, slugs and
branches are matched against regexes; titles and answers are one line that
cannot start with `-`; the repository must be in `fleet.repos`; `publish` is an
enum. The brief is written by the plugin from four sections, size-capped, to a
mode-600 file. Every subprocess runs without a shell, with a timeout, and with
`SLACK_*`, `THURBOX_SESSION` and `THURBOX_TASK` dropped from its environment,
so a task can never record the gateway's launcher as its origin.

**What Vega never does.** It never runs `abandon`, `block`, bare `dispatch` or
anything touching another origin's tasks. `vega_send` accepts only refs that
`find --origin` lists for this thread, at most `origin.max_sends_per_hour` per
task. There is no file, terminal or web tool.

## Repository layout

The repo is a Hermes **profile distribution**: `hermes profile install` copies
exactly the paths named in `distribution.yaml` into a `vega` profile.

| Path | What it is |
| --- | --- |
| `distribution.yaml` | Hermes distribution manifest: the owned paths and the env vars the profile needs |
| `SOUL.md` | Vega's persona |
| `config.yaml` | Hermes config for the profile: enables the plugin, restricts the Slack toolset, sets threading and mention rules |
| `plugins/vega/` | The Hermes plugin: hooks, tools, the origin logic, the heartbeat glue. It is also the `vega` Python package |
| `plugins/vega/origin.py` | Thread records, topic prompts, briefs, the dispatch gate, the news diff |
| `plugins/vega/heartbeat.py` | The one module that knows how a heartbeat is armed from a plugin |
| `plugins/vega/fleet.py` | Every fleet argv Vega runs, and the checks on model-supplied values |
| `skills/` | `fleet-origin` (points at fleet's `fleet-intake`) and `fleet-status` |
| `slack/manifest.yaml` | Slack app manifest: Socket Mode, minimum scopes |
| `vega.example.toml` | Every Vega setting with its default. The real `vega.toml` is never committed |
| `scripts/install.sh` | Installs or updates the profile, seeds `vega.toml`, installs the `vega` command |
| `scripts/run.sh` | Runs the gateway in the foreground |
| `scripts/demo_offline.py` | The offline end-to-end demonstration against a throwaway Hermes home and a scratch queue |
| `tests/` | pytest suite with stubbed `fleet` and Hermes, including an offline end-to-end run |

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

It never touches another Hermes profile.

### 2. Create the Slack app from the manifest

1. Go to <https://api.slack.com/apps>, choose **Create New App → From a
   manifest**, pick the workspace, and paste `slack/manifest.yaml`.
2. Under **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope. This is the `xapp-…` **app token**.
3. Under **Install App**, install it to the workspace, then copy the **Bot User
   OAuth Token** (`xoxb-…`).

### 3. Store the tokens in Vega's profile, never in the repo

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

Hermes prefers a `SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN` from the process
environment over the profile's `.env`. `scripts/run.sh` and `vega reply` strip
those variables before starting Hermes. If you start the gateway any other way,
do the same.

### 4. Fill in the allowlist and the repositories

In `~/.hermes/profiles/vega/vega.toml`:

```toml
[access]
users = ["U…", "U…"]   # Slack member ids: profile → ⋮ → Copy member ID
channels = ["C…"]      # channels where Vega answers @mentions, or ["*"] for any it is invited to

[fleet]
project = "~/fleet"    # the control-plane checkout
repos = ["~/app"]      # repositories a thread may open a task in; empty refuses every task
```

Check the result with `vega config`. With an empty allowlist, Vega refuses
everyone; with an empty `repos`, every `vega_task_add` is refused.

Also restrict Hermes' own slash commands, such as `/model` and `/reset`, to
yourself. The ids belong in the profile, not the repo:

```bash
hermes -p vega config set platforms.slack.extra.allow_admin_from '["U…your id…"]'
hermes -p vega config set platforms.slack.extra.user_allowed_commands '[]'
```

### 5. Pick Vega's model

```bash
hermes -p vega model      # any provider Hermes supports
```

### 6. Start Vega

```bash
scripts/run.sh            # foreground: `hermes gateway run`, which serves the vega profile
# or, to keep it running across logins:
hermes gateway install && hermes gateway start
```

Hermes runs one host gateway per machine, and it serves every profile, Vega's
included. `hermes -p vega gateway status` shows whether Vega is being served.

Then invite `@Vega` to an allowlisted channel and ask it for something there.

### 7. What fleet needs

Nothing. The origin tools run `uv run --project <fleet.project> fleet …` from
the gateway's own process, which needs only `uv` and `thurbox-cli` on its
`PATH` (a gateway installed with `hermes gateway install` inherits the user's
`~/.local/bin`). The queue is anchored on the checkout, not on the process cwd.
Workers' questions reach the thread through `declare needs-input`, which the
heartbeat reads from the task's `progress.jsonl`; no `--reply` command and no
`owners.conf` rule is needed.

## Trying it without Slack

The `vega` command runs the same code paths as the plugin, with no Slack
needed.

```bash
export VEGA_CONFIG=/path/to/test-vega.toml      # with the fake ids below in [access]
vega admit --user U0DEMOUSER1 --name "Demo Teammate" --channel C0DEMOCHAN1 \
  --thread-ts 1700000000.000100 "Please add a retry to the nightly export job"
vega threads                                    # the thread record it wrote
vega news C0DEMOCHAN1/1700000000.000100         # what fleet says about its tasks
vega status fuel                                # one read-only fleet view
vega reply C0DEMOCHAN1/1700000000.000100 "On it." --dry-run   # prints, posts nothing
```

`vega admit` runs the allowlist and writes the thread record exactly as the
gateway hook does. The intake itself (`topic add`, `add`, `dispatch`) is the
model's to run through the tools; `tests/test_end_to_end_offline.py` drives the
whole path with fleet and Hermes stubbed.

`scripts/demo_offline.py` goes further: run with the gateway's own Python against
a throwaway `HERMES_HOME` and a scratch queue (`FLEET_QUEUE_DIR`), it drives the
real Hermes session store, heartbeat row, restore scan and poll step, and real
`fleet queue topic add`, `add`, `find`, `show` and `declare`, with only `dispatch`
(`--dry-run`) and `send` held back. Its docstring says how to run it.

## Configuration

`vega.example.toml` documents every key. The ones that matter most:

| Key | Default | Meaning |
| --- | --- | --- |
| `access.users` | `[]` | Slack member ids allowed to talk to Vega |
| `access.channels` | `[]` | Channel ids where Vega answers, or `["*"]` for any channel it is invited to |
| `fleet.project` | `~/fleet` | The fleet checkout every command runs against |
| `fleet.repos` | `[]` | Repositories a thread may open a task in |
| `fleet.fuel_margin` | `10` | Dispatch only at `reserve + margin` or more, fresh |
| `fleet.fuel_providers` | `[]` | Accounts the gate reads; empty means the first one printed |
| `origin.heartbeat_interval` | `5m` | How often a thread with open tasks checks for news |
| `origin.max_sends_per_hour` | `6` | Cap on `vega_send` per task |
| `runtime.dir` | `~/.local/state/vega` | Thread records, prompts and briefs (mode 600) |
| `slack.hermes_profile` | `vega` | Profile whose tokens `vega reply` uses |

Vega looks for its config in this order: `--config`, `$VEGA_CONFIG`, the Hermes
profile home (`$HERMES_HOME/vega.toml`), then `~/.hermes/profiles/vega/vega.toml`.

## Security notes

- **Refusals only when addressed.** Someone off the allowlist is refused only
  when they DM Vega or @mention it. Their ordinary replies in a thread Vega is
  part of are dropped silently.
- **Fails closed.** If the access check raises, the message is dropped.
- **Checked twice, then again.** Hermes opens its own allowlist so strangers
  can be refused politely. Vega checks in `pre_gateway_dispatch`, again at
  admission, and inside every tool.
- **No commands from Slack.** Nothing from Slack reaches a command line except
  as a validated ref, slug, branch, title or one-line answer, or inside a file
  fleet reads. Every subprocess runs without a shell, with a timeout and a
  stripped environment.
- **One origin per thread.** A thread can dispatch, read and message only its
  own tasks.
- **Clean output.** Tool output has the home directory shortened to `~` before
  it reaches the model.
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

- `abandon` and brief edits from the thread. fleet-intake makes them the
  origin's, but they stay with the operator for now.
- Running Vega on a VPS. Socket Mode already needs no inbound port.
- A Keychain-backed token store. Hermes reads each profile's `.env`.
