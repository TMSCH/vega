---
name: mission-control-relay
description: Hand a teammate's Slack request to Mission Control, the fleet's lead session, which decides, dispatches and answers in the same Slack thread.
version: 0.1.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fleet, relay, slack]
    requires_tools: [vega_relay_to_mission_control]
    related_skills: [fleet-status]
---

# Relay to Mission Control

Requests for work normally never reach you: Vega's plugin relays them before a
model turn starts. Use this skill for the rest — a message that looked like a
status question but asks for something you cannot read from fleet's views.

1. Call `vega_relay_to_mission_control` with the person's message **verbatim**.
   Do not summarise, translate or add your own reading of it. The tool attaches who
   sent it and which Slack thread to answer in.
2. On success, reply with one line: it has been passed to Mission Control, which
   will answer in this thread.
3. On error, say Mission Control could not be reached right now and nothing was
   passed on, so they should try again in a few minutes. Do not retry in a loop.

## How the answer comes back

Mission Control replies itself, with `vega reply <channel>/<thread_ts> "<text>"`
on its own machine, using the target in the `[from: … · slack: …]` prefix the relay
added. You do not wait for it and you do not forward it.
