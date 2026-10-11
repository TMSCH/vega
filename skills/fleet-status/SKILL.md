---
name: fleet-status
description: Answer a teammate's question about what the fleet is doing (what's running, what's queued, how a topic is going) from fleet's read-only views, without waking Mission Control.
version: 0.1.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fleet, status, slack]
    requires_tools: [vega_fleet_status]
    related_skills: [mission-control-relay]
---

# Fleet status

Use this when someone in Slack asks what is happening in the fleet rather than
asking for work to be done.

1. Pick the view:
   - `status` — the one-glance reading: provider fuel, then every live topic with
     its tasks and their states. Start here for "what's running?".
   - `queue` — topics and tasks re-checked against their pull requests (state,
     checks, review). Use it for "is X merged?", "what's blocked?".
   - `context` — where each open topic stands, what is ready and what stalled.
2. Call `vega_fleet_status` with that view. Call a second view only if the first
   does not answer the question.
3. Answer in at most six short lines. Lead with the answer. Name topics and tasks
   as the output names them; include pull request links the output gives.
4. Do not mention provider fuel or account windows unless asked.
5. If the question needs judgement (priorities, "should we…", "when will…"), or the
   views do not answer it, use the `mission-control-relay` skill instead.

The views are read-only by construction: the tool runs one of three fixed fleet
commands and nothing else. You cannot change the queue, and you do not offer to.
