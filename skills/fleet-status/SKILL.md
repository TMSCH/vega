---
name: fleet-status
description: Answer a teammate's question about what the fleet is doing (what's running, what's queued, how a task is going) from fleet's read-only views.
version: 0.2.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fleet, status, slack]
    requires_tools: [vega_fleet_status, vega_find, vega_show]
    related_skills: [fleet-origin]
---

# Fleet status

Use this when someone asks what is happening rather than asking for work.

1. Pick the view for `vega_fleet_status`:
   - `status`: the one-glance reading: fuel, then every live topic with its
     tasks and their states. Start here for "what's running?".
   - `fuel`: remaining fuel per account, with the reserve. For "can we start
     more?".
   - `queue`: topics and tasks re-checked against their pull requests. For "is X
     merged?", "what's blocked?".
   - `plan`: what is ready, stalled, unprompted or waiting on input.
   - `context`: where each open topic stands, what is ready and what stalled.
2. For this thread's own work, `vega_find` lists its tasks and `vega_show`
   prints one task's record. Prefer them to the global views when the question
   is about "my request".
3. Answer in at most six short lines. Lead with the answer. Name topics and
   tasks as the output names them; include pull request links the output gives.
4. Do not mention provider fuel or account windows unless asked.
5. A question that asks for a change (start, stop, abandon, re-prioritise) is
   the `fleet-origin` skill's, or the operator's.

The views are read-only by construction: the tool runs fixed fleet commands and
nothing else.
