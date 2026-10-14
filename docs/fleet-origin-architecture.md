# Vega as a fleet origin

This doc covers how to move Vega from "relay every Slack request to Mission
Control" to "each Hermes conversation is its own fleet origin". It is research
only: nothing in Vega changes with it.

**Target.** A Slack thread starts a Hermes conversation, and that conversation
runs fleet's intake itself:

1. `fleet queue topic add`.
2. `fleet queue add --origin <its own id> --reply '<command that reaches it>'`.
3. Write the briefs.
4. Read fuel, then `fleet queue dispatch --origin <its own id>`.

Workers and the Conductor answer through the recorded `--reply`, and that reply
must come back into the same Hermes conversation as a new turn, not as a direct
Slack post. Mission Control leaves Vega's path.

**What was read.**

- Hermes Agent v0.22.0 source: paths below are relative to the hermes-agent
  checkout.
- fleet `main` at #238: `.agents/skills/fleet-intake`, `.agents/skills/fleet-conductor`,
  `orchestration/owners.example.conf`, `AGENTS.md`, `scripts/lib/queue.py`,
  `scripts/lib/notify_origin.py` and `scripts/lib/notify_delivery.py`.
- This repo at `main`.

**How it was checked.**

- Hermes `--help` output came from a throwaway `HERMES_HOME`, since deleted.
- Nothing posted to Slack.
- Nothing went through the running Vega profile.
- No fleet command that writes to the queue was run. The only fleet commands
  run were `fleet status --fuel` and `fleet queue find --origin … --json`, both
  read-only.

**Markers.** Every claim carries one:

- **[V]** — verified, read in source or observed from a command.
- **[I]** — inferred, not exercised end to end.

## Today, in one paragraph

Two hooks in `plugins/vega/hermes_glue.py` do the work.

- `pre_gateway_dispatch` refuses senders who are not on the allowlist.
- `post_gateway_admission` hands every other admitted message to
  `inbound.handle`.

`inbound.handle` sends the message to `relay.relay`, unless it is a status
question. `relay.relay` takes the one live thurbox session whose name contains
`mission_control.lead_match` and runs `thurbox-cli message send --to <id>
--kind slack` with a `[from: … · slack: C…/ts]` line. No model turn runs **[V]**.

Mission Control answers with `vega reply C…/ts "<text>"`, which is
`reply.post` → `hermes -p vega send --to slack:C…:ts`. That posts to Slack and
stops **[V]**. It runs no turn and only mirrors the text into the thread's
transcript (`hermes_cli/send_cmd.py:194-241`, `tools/send_message_tool.py:281`)
**[V]**.

The Slack model carries exactly `vega_fleet_status` and
`vega_relay_to_mission_control` (`config.yaml` `platform_toolsets.slack: [vega]`)
**[V]**.

## 1. Conversation identity

Hermes has two identifiers, and only one of them is stable.

**The session key.** It is a routing address, built by `build_session_key`
(`gateway/session.py:685-724`) **[V]**:

```
<ns>:<platform>:<chat_type>[:<slack team id>][:<chat_id>][:<thread_id>][:<user>]
```

- `<ns>` is `agent:<profile>` when the gateway multiplexes profiles, and
  `agent:main` otherwise (`session.py:656-665`) **[V]**. With multiplexing, a
  Vega key starts `agent:vega:` **[V]**.
- The Slack adapter sets `chat_id` = channel, `chat_type` = `group` for
  channels and `dm` for `im`/`mpim`, `thread_id` = thread_ts and `scope_id` =
  team id (`plugins/platforms/slack/adapter.py:4704-4714`) **[V]**.
- With the default `thread_sessions_per_user: false`, a thread is shared by
  everyone in it, so no user part is appended (`session.py:711`) **[V]**.

The keys that result:

| Slack message | Session key |
| --- | --- |
| Reply in a channel thread | `agent:vega:slack:group:<team>:<channel>:<thread_ts>` |
| Top-level channel message, `reply_in_thread: true` (Vega's `config.yaml`) | The same shape, with the message's own ts as the thread (`adapter.py:4295-4325`), so the bot's first reply and every later reply in that thread share one key |
| DM | `agent:vega:slack:dm:<team>:<D…>:<thread_ts or ts>`; `dm_top_level_threads_as_sessions` defaults to on |

The thread and DM keys are **[V]**.

**The session id.** It names one conversation transcript:
`YYYYmmdd_HHMMSS_<8 hex>` (`hermes_state_ids.py:56-60`) **[V]**. A key points
at exactly one id at a time, through the `gateway_routing` table in `state.db`
(`hermes_state_common.py:509-515`), mirrored in `sessions/sessions.json`
**[V]**.

**Stable across restarts?**

- **The key: yes**, by construction, since it is computed from the Slack
  thread **[V]**.
- **The key → id mapping also survives a restart**: it is in `state.db`, and
  a missing entry is recovered from the `sessions` table by key
  (`session_recovery.py:255-294`) **[V]**.
- **The id is not stable for the life of a thread.** These change it **[V]**:
  - `/new` or `/reset` (`session.py:1139-1158`);
  - `/stop` followed by a new message (`session_lifecycle.py:116-121`,
    `session.py:1003-1016`);
  - `/resume` (`session.py:1246`);
  - context compression, which moves the key to a child id
    (`gateway/session_transcript.py:136`).
- Time-based resets were retired in this release: "gateway conversations reset
  only on /new or /reset" (`hermes_cli/session_reset_retirement.py`) **[V]**.

**The Slack thread → conversation mapping.** One Slack thread is one session
key, which is one live Hermes conversation **[V]**.

**What fleet's origin should be.** It should be **the Slack thread, not the
Hermes session id**: `slack:<channel>/<thread_ts>`, the same string the
abandoned `feat/slack-lead` branch used.

- The thread outlives every id rotation.
- The session key is derivable from it, given the team id.
- Fleet treats any non-`thurbox:` origin as "always reachable: a chat thread
  does not disappear" (`queue.py` `OriginTarget.reachable`) **[V]**, which is
  exactly true of a thread and false of a session id.

## 2. Re-entry: waking that conversation from outside

Hermes ships no CLI or HTTP endpoint that adds a turn to an existing gateway
conversation **[V]**. The plugin docs say so directly: "This plugin API does
not expose a public HTTP endpoint or CLI command for external processes"
(`website/docs/user-guide/features/plugins.md:976`).

What exists:

| Mechanism | New turn in that Slack session? | Evidence |
| --- | --- | --- |
| `ctx.inject_message(content, session_key=… \| origin=…)`, plugin API | **Yes**, in-process only | `hermes_cli/plugins.py:603`, `hermes_cli/plugins_injection.py`, `gateway/run_plugin_injection.py` **[V]** |
| `hermes send` | No: posts, and mirrors the text as an assistant row | `send_cmd.py:194-241` **[V]** |
| Gateway control socket (`$HERMES_HOME/gateway.sock`) | No: fixed admin verbs, with no message verb and no plugin extension | `gateway/control_socket.py`, verbs at `gateway/run.py:5603-5612` **[V]** |
| `hermes webhook` | No: every POST is a fresh one-shot session, and `--mirror-to-session` only appends text | `gateway/platforms/webhook.py:87-95, 711-739, 957-982` **[V]** |
| API server `X-Hermes-Session-Id` | Runs a separate agent on that history; the reply comes back over HTTP, not Slack, and without Slack's toolset limits | `api_server_openai_routes.py:686-711` **[V]** |
| `hermes chat --resume <id> -q` | Appends to the transcript; output goes to stdout only, under CLI toolsets | `agent/turn_facade_lease.py` **[I]** |
| Kanban `notify-subscribe --delivery-mode wake` | Wakes the thread, but with fixed template text that keeps 200 characters of the payload | `gateway/kanban_watchers_notifier.py:392, 670` **[V]**; end to end **[I]** |

### How `inject_message` behaves

- **Opt-in.** It needs `plugins.entries.vega.allow_gateway_injection: true` in
  the profile's `config.yaml`, and refuses with a warning otherwise
  (`plugins_injection.py:33`, `plugins.py:630`) **[V]**.
- **Two ways to target:**
  - **`session_key`** is *strict*: it pins the current session id and drops
    the event if the id rotated before dispatch (`run_plugin_injection.py:126-160`,
    `gateway/run_turn.py:433-441`) **[V]**.
  - **`origin`** is a `SessionSource.to_dict()`-shaped mapping (platform,
    chat_id, chat_type, thread_id, user_id, scope_id). It is *non-strict* and
    "start[s] (or continue[s]) the session at a plugin-supplied origin",
    whatever id the thread routes to now (`run_plugin_injection.py:218-243`)
    **[V]**. **This is the one Vega wants**, because it survives `/new` and
    compression.
- **Idle session:** the turn starts at once **[V]**.
- **Mid-turn session:** the turn is *queued*. It never interrupts or steers,
  whatever `busy_input_mode` is set to: "Internal synthetic events … must never
  interrupt or steer; they surface as a NEW turn when idle. Plugin events …
  queue them through the FIFO" (`gateway/run_busy.py:846`) **[V]**. The
  per-session queue is capped at 32 (`gateway/run.py:3969`) **[V]**.
- **Refused:**
  - while the gateway is draining or restarting
    (`_plugin_injection_accepting`, `run_plugin_injection.py:98`) **[V]**;
  - when the stored source fails today's core authorization
    (`_plugin_injection_authorized`) **[V]**. With Vega's
    `SLACK_ALLOW_ALL_USERS=true`, that core check always passes **[I]**.
- **Accepted is not delivered.** `True` means "accepted for async dispatch".
  A later drop (unknown key, rotated id, no adapter) is only logged **[V]**.
- **Vega's own hooks never see it.** An internal event returns before
  `pre_gateway_dispatch` (`gateway/run_inbound.py:226`), and
  `post_gateway_admission` runs only `if not is_internal` (`run_inbound.py:1362`)
  **[V]**. So a reply is neither relayed nor refused, and **Vega's allowlist
  does not re-check it**. `vega say` must do that check itself.
- **It cannot act as a control message.** It cannot run slash commands
  (`allow_gateway_control=False`) or answer an approval prompt
  (`run_inbound.py:1222`) **[V]**.
- **The reply lands in the thread.** The turn runs on the Slack adapter and
  answers in the stored thread **[V]** for the code path; **[I]** end to end,
  since no Slack was touched.

### What `--reply` would be

Hermes gives no external entry point, so Vega has to add one. The plugin
already runs inside the gateway. It would:

1. Open a unix socket inside the gateway process, for example
   `$HERMES_HOME/…/vega-say.sock`, mode 0600, checking the peer uid. This is
   the same trust model as Hermes' own `control_socket.py`.
2. Accept one JSON line per connection: `{"origin": "slack:C…/ts", "text": "…"}`.
3. Rebuild the Slack source for that thread and call
   `ctx.inject_message(text, origin=…)`.

The rebuilt source needs channel, thread_ts, team id, chat type and a user id
for the authorization check. Vega would remember the source of each thread it
admitted, for example in `<runtime dir>/threads/`.

A `vega say` command is the client:

```
vega say slack:C0123ABCD/1712345678.123456 "<text>"     # or --file <path>
```

This works from a worker's shell in another worktree **[I]**, for these
reasons:

- `vega` is a `uv tool` on the user's PATH **[V]**.
- The socket is addressed by absolute path, so the caller's cwd does not
  matter.
- Workers on this machine run as the same uid as the gateway **[V]**.

Whether the plugin may bind a socket in-process, and whether `inject_message`
called from that socket thread reaches the loop, is **[V]** from the code
(`safe_schedule_threadsafe`, `run_plugin_injection.py:38`) but untested.

**One argument shape for both routes.** The command takes the origin first and
the text last. That is exactly the shape of an `owners.conf` relay, which fleet
runs "with the origin and the message appended as its last two arguments"
(`orchestration/owners.example.conf`, `queue.py` `relay_argv`) **[V]**. So both
of these produce the same call **[V]** for the argv shapes:

- the per-task reply: `--reply 'vega say slack:C…/ts'`, run by the worker as
  `vega say slack:C…/ts '<text>'`;
- a relay rule: `slack: = !vega say`.

**When it fails:**

- `vega say` exits non-zero when the socket is absent (gateway down), when the
  thread is unknown, or when injection is refused.
- fleet's brief then tells the worker: "If that command fails, the requester
  cannot be reached: ask … here … and `declare needs-input`"
  (`queue.py` `reply_lines`) **[V]**.
- A worker on a `--host` machine cannot reach the socket, so its replies
  always take that fallback **[I]**.

**The text should be labelled.** Inject the text with a prefix such as
`[fleet · <ref>]`. It then reads as a fleet message and not as the person who
started the thread. The injected turn is attributed to the stored origin user
(`run_plugin_injection.py` uses the restored source) **[V]**.

## 3. Tools for the intake flow

The Slack model has no terminal today **[V]**. Hermes' built-in options are too
wide:

- **The `terminal` toolset cannot be limited to an allowlist.** The guard runs
  hardline blocks, then `approvals.deny`, then `mode=off`, then
  `command_allowlist`, then dangerous-pattern detection, and **anything not
  flagged as dangerous runs unprompted** (`tools/approval.py:1128-1171`,
  line 1164) **[V]**.
  - `command_allowlist` only skips prompts.
  - `approvals.deny` is fnmatch with no negation, so it cannot say "only
    these" (`tools/approval_floors.py:23-43, 188-204`) **[V]**.
  - The toolset also brings `process_manage` (`toolsets.py:102`) **[V]**.
- **The `file` toolset is all or nothing.** It bundles read, write, patch and
  search together. Its only root limit, `HERMES_WRITE_SAFE_ROOT`, is
  process-wide (`toolsets.py:122-125`, `agent/file_safety.py:222-228`) **[V]**.
- **Skills cannot run their own scripts** without `terminal` or `execute_code`
  (`tools/skills_tool.py`) **[V]**.
- **Toolsets cannot be scoped per Slack channel.** Only the webhook adapter
  implements `toolsets_for_source` (`gateway/run_turn.py:2387-2403`) **[V]**.

**The narrowest tool set is plugin tools that build fixed argv and run it with
no shell**, the pattern `vega_fleet_status` already follows. Each tool runs
`uv run --project <fleet.project> fleet …`:

| Tool | Command | The model supplies |
| --- | --- | --- |
| `vega_fleet_status` (exists) | `fleet status`, `queue list --live`, `context`; add `status --fuel`, `queue plan`, `queue show <ref>` | the view, and a ref matching `^[a-z0-9-]+/\d\d-[a-z0-9-]+$` |
| `vega_topic_add` | `fleet queue topic add <slug> --title T --prompt-file <file> --by <name>` | slug, title. Vega writes the prompt file from the Slack message verbatim, and `--by` comes from the session's user name |
| `vega_task_add` | `fleet queue add <topic> <slug> --repo P --branch B [--base] [--add-dir P] [--publish M] --title T --brief-file F --origin <origin> --reply 'vega say <origin>'` | topic, slug, repo, branch, title and the brief text. **`--origin` and `--reply` are never the model's**: the handler builds them from the session (`HERMES_SESSION_CHAT_ID`/`_THREAD_ID`) |
| `vega_dispatch` | `fleet queue dispatch --origin <origin>` | nothing |
| `vega_find` | `fleet queue find --origin <origin> --json` | nothing |
| `vega_send` | `fleet queue send <ref> '<answer>'` | the answer, and a ref that `find --origin` lists for this thread |

Notes on that table:

- **Briefs are written by the handler,** not by a file tool, to a 0600 file
  under Vega's runtime dir, size-capped, then passed as `--brief-file`.
  `add` refuses a brief that leaves any of its four sections unwritten, so a
  bad brief fails at add time (`queue.py` usage) **[V]**.
- **Running "from the fleet checkout".** `topic add` and `add` refuse only when
  the *code's* checkout is not the control plane (`guard_creating`,
  `foreign_checkout`). That is anchored on `queue.py`'s own path, not the
  process cwd (`queue.py:864-875`, `1462-1499`) **[V]**. So
  `uv run --project <control-plane checkout>` from the gateway's cwd passes
  **[V]** in the code; it was not exercised, since that would write. Vega
  already runs fleet this way (`plugins/vega/fleet.py`) **[V]**.
- **The fuel rule.** `fleet status --fuel` prints a tab-separated record per
  provider with `remaining`, `reserve`, `stale` and `state` **[V]**, observed.
  fleet-intake says an origin dispatches only when every account the tasks
  will spend reads **at least ten points over the reserve, fresh** **[V]**.
  The handler of `vega_dispatch` should enforce that itself — read fuel,
  refuse below `reserve + 10`, or when `stale`/`unavailable` — rather than
  trust the model. Below the line, the tasks stay `queued` and the conductor
  decides.
- **Confirming before many workers.** fleet-intake asks an origin to confirm
  with the person in one line before starting more than two workers **[V]**.
  That is a SOUL/skill rule for the model.

**Where the safety boundary sits:**

- **No shell, ever.** Fixed argv; free text goes only into `--title`,
  `--prompt-file` and `--brief-file` contents, or after the ref in `send`.
  Reject any model-supplied value that starts with `-`.
- **No path from the model,** except `--repo`/`--add-dir`. Those are checked
  against fleet's registry or an allowlist in `vega.toml`.
- **The origin is fixed per thread.** A conversation can dispatch, find and
  `send` only to its own tasks. It cannot abandon, block, reap or touch another
  origin's work. `abandon` and brief edits are the origin's per fleet-intake,
  but they can stay with the operator in the first phase.
- **A minimal subprocess environment.** A plugin's own `subprocess.run`
  inherits the gateway's environment **[I]**. Hermes scrubs Slack tokens only
  from *its* terminal tool (`tools/environments/local.py:242-307`) **[V]**.
  So the handler should pass only `PATH`, `HOME` and what `uv`/`gh` need,
  dropping `SLACK_*` (as `reply.py` does), and `THURBOX_SESSION`/`THURBOX_TASK`
  (as `relay.py` does). Without the `THURBOX_SESSION` drop, `add` would default
  the origin to whatever session launched the gateway (`queue.py` `add_origin`)
  **[V]**. A gateway started from inside a thurbox session would carry it.
- **The allowlist is re-checked inside every tool**, as `_session_allowed`
  already does **[V]**.
- **Optional isolation:** `plugins.isolation: host` runs plugin code in a
  separate per-profile process (`hermes_cli/plugin_isolation.py`) **[V]**.
  Whether `inject_message` and an in-plugin socket still work under host
  isolation is **unknown**.

`dispatch` from a process outside any thurbox session spawns workers with no
`--parent` (`queue.py:4892`) **[V]**. That is cosmetic (the session list's
nesting). Whether `dispatch` has any other dependency on running inside thurbox
was not exercised **[I]**: it needs `thurbox-cli` on the gateway's PATH.

## 4. Delivering an outcome or question to the origin

Today the reconciler tells an origin two things: the task's outcome (`shipped`,
then `landed`/`abandoned`) and a worker's `needs-input` question
(`notify_origin.py`) **[V]**. It finds the origin only through `origin_target`.
That resolves a thurbox session origin itself, and any other origin through an
`owners.conf` rule. **No notifier reads the task's `reply`** (no use of it in
`notify_origin.py`/`notify_delivery.py`) **[V]**. A `slack:` origin with no
rule is `unrouted`: its question stays with the conductor, and "its outcome is
told to nobody" (`owners.example.conf`) **[V]**.

**(a) Vega installs a relay rule** in the operator's `orchestration/owners.conf`:

```
slack: = !vega say
```

- fleet runs `vega say <origin> <line>` with no shell, killed after 30 s
  (`notify_delivery.RELAY_SECS`). A failed outcome relay is retried 3 times
  and then dropped (`notify_origin.RELAY_TRIES`) **[V]**.
- **What Vega needs:** `vega say` and the socket, documented as an operator
  step, and a `vega say` that fits in 30 s. Nothing in fleet changes.
- **Costs:**
  - A rule is per prefix, not per task, so every `slack:` origin on this fleet
    reaches Vega. That is fine, since Vega owns `slack:`.
  - The rule lives in fleet's gitignored file, so it is a second thing to
    install.
  - The reconciler runs as its own user service, so `vega` must be on *its*
    PATH too **[I]**.
  - The question line carries the fleet checkout's absolute path
    (`question_line`), so the Slack model must not echo it. `fleet.scrub`
    shortens `$HOME` already.

**(b) fleet uses the task's recorded `--reply`** when no rule matches. This is a
fleet change not made yet.

- The notifier would run `shlex.split(reply) + [line]` bounded, as it does for
  a relay.
- **What Vega needs:** only the `--reply` it already records at `add`. No
  `owners.conf` step, and any number of front ends each carry their own route.
- **What fleet must decide:**
  - Whether running a recorded command from the reconciler is acceptable. A
    task record is written by any origin, while `owners.conf` is the
    operator's.
  - How it tells "gone" for a non-session origin. Today that origin is always
    reachable.
  - Whether the default `thurbox-cli message send` reply should keep going
    through the session-delivery rules (at rest, empty input line) rather than
    run blind.

**Recommendation:** use (a) now. It works on fleet `main` as it is. Raise (b)
with fleet as the long-term shape, so that `--reply` is the one road for
workers, conductor *and* reconciler. Both call the same `vega say`, so the
switch costs Vega nothing.

The Conductor reaches an origin directly only for a not-dispatched task: it
blocks the task with a condition, then "run[s] the task's recorded `reply`"
(`fleet-conductor` SKILL) **[V]**. That already works with
`--reply 'vega say …'`. For a dispatched task, it `declare`s needs-input,
which takes route (a) or (b).

## 5. Mission Control lookup after the change

Several things in Vega still find a session by display name:

- `relay.resolve_lead` with `mission_control.lead_match`: a substring match
  over `thurbox-cli session list`, used by `relay`, `inbound.handle`,
  `vega_relay_to_mission_control` and `vega relay` **[V]**. In the target
  architecture **all of it goes**: no message is relayed to a lead.
- `mission_control.ack`, `reply_hint` and the `[from: …]` header exist only for
  the relay, and go with it.
- The SOUL, README and the `mission-control-relay` skill name Mission Control.

What remains:

- **Nothing in Vega needs to find the conductor.** fleet finds it itself
  (`notify_conductor.conductor_row`).
- **A lead/conductor fallback,** if Vega keeps one, for "a request I cannot take
  in" during rollout. It should name the session by a stable key, not a
  substring. That is the `fleet.<fleet name>.<role>` key being designed
  separately in fleet: Vega would hold `fleet.<name>.conductor` (or `.lead`) in
  `vega.toml` and resolve it through whatever that design exposes. **Vega
  depends on that design here and should not invent its own.**
- **A relay rule in `owners.conf`** names a command, not a session, so
  route (a) has no name lookup at all **[V]**.
- **An origin is addressed by its own key:** `slack:<channel>/<thread_ts>` →
  the Slack source Vega recorded → the Hermes session the gateway routes it to.
  No display name is involved anywhere.

## 6. Concurrency and failure

- **Many conversations at once.** Each thread is its own session key, so its
  own lane, transcript and turn **[V]**. Two of them dispatching together can
  both read fuel above the line. fleet's answer is the ten-point margin plus
  the per-task `dispatch.lock` ("being dispatched by another process") **[V]**.
  `dispatch --origin` keeps each conversation to its own tasks **[V]**. The
  gateway's model calls are shared across threads, which is a Hermes and
  provider limit, not fleet's.
- **A conversation that resets (`/new`), compresses or restarts with tasks
  still running.**
  - The origin is the thread, so `find --origin` still lists its tasks.
  - `inject_message(origin=…)` continues whichever id the thread routes to
    now **[V]**.
  - The new id has no memory of the intake. So the injected text must stand on
    its own: task ref, what happened, and the command that answers. fleet's
    notice lines already do this **[V]**.
  - The model should start a turn that mentions a ref by running `vega_find`
    / `queue show`.
- **The gateway is down or restarting.**
  - `vega say` gets no socket and exits non-zero.
  - The worker falls back to asking in its pane and declaring `needs-input`
    **[V]** (brief text).
  - A relayed outcome is retried on the next `collect` passes, then dropped
    after 3 tries **[V]**.
  - A dropped outcome stays in the task record (`fleet queue show`) but is not
    posted. Vega could post a catch-up for a thread from `find --origin` on its
    next message there **[I]**.
- **A worker replying to a conversation that is gone.**
  - A Slack thread does not go away, so "gone" means one of three things:
    Vega no longer knows the thread (runtime state lost), the thread's user is
    no longer allowed, or the channel was archived or Vega removed.
  - `vega say` should fail loudly in the first two cases, so the worker takes
    the `needs-input` road and the conductor keeps the question on the board.
  - In the last case, the turn runs and its Slack post fails. That shows only
    in the gateway log **[I]**.
- **A session queue that fills up.** A conversation busy in a long turn
  accumulates injected turns in FIFO order, capped at 32 **[V]**. Each runs
  separately when idle, so a chatty worker costs one model turn per line.
  Workers should reply only at the points the brief lists.
- **Loops.** An injected turn skips Vega's hooks, so it is never relayed
  **[V]**. The model could still answer a worker by `vega_send`, which reaches
  the worker without a wake (`fleet queue send`) **[V]**. A ping-pong is
  bounded by the 32-deep queue and by model turns, not by design. A per-task
  cap on `vega_send` per hour is cheap insurance.

## Recommended phased plan

**Phase 0 — prove re-entry.** Vega only, behind a flag.

- Add `allow_gateway_injection` to the profile config template.
- Add the plugin socket.
- Record each admitted thread's source under the runtime dir.
- Add `vega say <origin> <text> [--file]`.
- Test against a throwaway profile and a test workspace or channel. Inject
  while idle, and again while mid-turn, and confirm the reply lands in the
  thread.

This settles the open items marked "untested" below.

**Phase 1 — origin tools, still relaying by default.**

- Add `vega_topic_add`, `vega_task_add` (fixed `--origin`/`--reply`),
  `vega_dispatch` (fuel-gated in code), `vega_find`, `vega_send` and the extra
  status views, with a minimal subprocess env.
- Add a `fleet-intake`-shaped skill that points at fleet's
  `.agents/skills/fleet-intake/SKILL.md` instead of copying it.
- Gate the tools per channel in `vega.toml`, so one test channel runs as an
  origin while the others still relay.
- Operator adds `slack: = !vega say` to `owners.conf`.

**Phase 2 — cut Mission Control out.**

- Make origin mode the default.
- Delete `relay.py`'s lead lookup, `vega_relay_to_mission_control`, the
  `mission-control-relay` skill, `[mission_control]` config, and the
  README/SOUL text about relaying.
- `vega reply` stays as an operator escape hatch, or is removed.
- A request Vega cannot take in is answered in-thread. If a fallback session
  is wanted, it is named by the `fleet.<name>.<role>` key, once that exists.

**Phase 3 — fleet change, optional.**

- `notify_origin` falls back to the task's recorded `reply` for an unrouted
  origin (route (b)).
- Then Vega drops the `owners.conf` step. Nothing else in Vega changes.

**Order of dependencies:** Phase 0 blocks everything. Phase 1 needs nothing
from fleet. Phase 2's fallback (if any) waits on the session-key design.
Phase 3 is fleet's to schedule.

## Open items: what this could not determine

1. **End-to-end injection into a real Slack thread.** Idle and mid-turn are
   read from code only. It needs a test workspace or channel and a throwaway
   profile; nothing here touched Slack.
2. **A socket inside the plugin:** binding it from a Hermes plugin, with
   `plugins.isolation` both `in_process` and `host`. There is no gateway-start
   hook, and plugins also load in CLI processes. The socket must therefore
   open lazily or only when an injector is live, and the public way to ask
   that was not found (`manager.has_gateway_message_injector` is private).
3. **`inject_message(origin=…)` for a thread Vega has never seen since
   restart,** that is, whether a source rebuilt from stored fields
   canonicalizes to the same key, team id included.
4. **Whether the session ContextVars reach plugin tool handlers' threads.**
   `get_session_env` says they do, while `terminal_tool.py:1405` notes the
   contextvar "doesn't cross tool-worker threads". Vega's tools already
   depend on it and work, which suggests yes **[I]**.
5. **`fleet queue add`/`dispatch` run from the gateway's service context.**
   This was not exercised, because it writes to the queue.
6. **Whether the reconciler's service PATH reaches `vega`** for route (a).
7. **The `fleet.<name>.<role>` key design,** which Vega's optional fallback
   depends on.
8. **Fleet's position on route (b),** a recorded command run by the
   reconciler.
