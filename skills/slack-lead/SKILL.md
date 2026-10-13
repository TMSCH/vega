---
name: slack-lead
description: Lead fleet work that began in Slack: make the topic and tasks with the thread as their origin, leave workers to answer in the thread, and report only what the queue says.
version: 0.1.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fleet, lead, slack]
    related_skills: [mission-control-relay, fleet-status]
---

# Lead Slack-origin work

Vega leads work that came from Slack; Mission Control leads work that came from
the terminal. This skill applies in a Vega session that can run `fleet queue`
(the operator's lead session, see "The lead session" in the README), not in the
read-only Slack gateway.

## Read the fleet's own rules first

Do not work from memory or a copy. Read, in the fleet checkout (`fleet.project`
in `vega.toml`):

- `FLEET.md`: what the lead is and how it behaves
- `AGENTS.md`: the repo's working rules
- `.agents/skills/fleet-queue/SKILL.md`: every `fleet queue` verb and the loop

Those files win over anything here. This skill adds only what is particular to
Slack.

## Making the work

For a Slack ask in `<channel>/<thread_ts>` (from the `[from: … · slack: …]`
prefix), file it from the fleet checkout:

```bash
uv run fleet queue topic add … --by "<name>"
uv run fleet queue add … \
  --origin 'slack:<channel>/<thread_ts>' \
  --reply  'vega reply <channel>/<thread_ts>'
```

Every task of the topic gets both flags. `--origin` is how a later message in
the thread finds the task's session; `--reply` is the command workers use to post
their own verdicts, pull-request notices and questions into the thread.

## Rules

- **Never answer a worker's work yourself.** Verdicts, "the PR is up", and
  questions to the asker come from the worker through `--reply`. You relay,
  schedule and unblock; you do not write the worker's findings.
- **Check before you claim.** Run `uv run fleet queue show <ref>` (or `list
  --live`) before saying a task is running, ready, blocked, stalled or done.
  Quote what it prints. Never infer state from memory of an earlier run.
- **Thread replies go to the live session.** The plugin routes a thread's
  messages to its task's session while that session lives; they reach you only
  when it is gone. If one does, read `queue show` and decide: answer from the
  record, or re-dispatch.
- Reply to the asker when you accept the request, and say plainly when something
  is stuck. Short, no preamble; never paste a token, a local path or a machine
  name into Slack.
- Post to Slack only with `vega reply`.
