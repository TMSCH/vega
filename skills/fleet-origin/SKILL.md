---
name: fleet-origin
description: Take a teammate's Slack request into fleet from this thread - open the topic, write each task's brief, dispatch when fuel allows, and report the news the heartbeat brings back. Use for any request for work; status questions are fleet-status.
version: 0.2.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [fleet, origin, intake, slack]
    requires_tools: [vega_topic_add, vega_task_add, vega_dispatch, vega_find, vega_send, vega_fleet_news]
    related_skills: [fleet-status]
---

# This thread is a fleet origin

The rules of an origin are fleet's, in its `fleet-intake` skill
(`.agents/skills/fleet-intake/SKILL.md` in the fleet checkout). This skill does
not restate them; it says how the steps map onto Vega's tools from a Slack
thread. Where the two differ, fleet's skill wins.

## 1. The request becomes a topic

Call `vega_topic_add` with a slug and a title. Vega writes the topic's prompt
from the person's messages in this thread, verbatim, and records who asked. Do
not paraphrase the request anywhere: the prompt file is the record.

## 2. One task per unit of work, each brief from scratch

Call `vega_task_add` once per task with the topic, a slug, a title, the
repository (one the operator allowlisted; the tool refuses any other), the
branch, and the four brief sections: **what to do**, **hard constraints**,
**coordination**, **done means**. Workers share no context with you or with the
thread, so each section must stand on its own. Vega sets the origin to this
thread; you never name it.

When the tasks span several repositories or would start more than two workers,
confirm the set with the person in one line before the first dispatch.

## 3. Dispatch, or leave it for the conductor

Call `vega_dispatch`. Vega reads fuel first and dispatches this thread's ready
tasks only when every gated account is clearly above the reserve. When it is
not, the result says so and the tasks stay queued: say that in one line, and
do not retry. The reconciler tells the conductor they are ready, and it
decides with one fuel reading.

## 4. Go back to the thread

Tell the person what was accepted: the topic, its tasks, what went out and what
was left queued. Then stop. What happens next reaches this thread through the
heartbeat Vega armed when the first task was added:

- Each tick, call `vega_fleet_news` once. It returns only what changed since the
  thread was last told: a worker started, a pull request opened, finished with
  the worker's result, shipped with the link, landed, abandoned and why, or a
  worker's question. Post each line; a result's numbers and facts stay exactly
  as the worker wrote them. With nothing new, reply exactly `NO_REPLY`.
- Answer a worker's question with `vega_send` when the answer is already in the
  thread. Otherwise ask the person and wait; the worker keeps its question on
  the board.
- When every task of the thread has landed or been abandoned, the heartbeat is
  cleared for you.

## What this thread does not do

- Dispatch another thread's tasks, or the whole queue. The tools cannot.
- Abandon, block or rewrite a task. Ask the operator in the thread.
- Edit fleet itself, or read any file: there is no file or terminal tool.
