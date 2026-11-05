# baton: one-shot by design

Spec and plan for items 6-24 of `docs/plans/2026-11-05-backlog.md`.

## Why

A 39-round job declared 30 FPS from a test that ran hidden, a person's 114 KB message sat undelivered
in OpenCode's queue for good, and messages that did arrive killed rounds mid-tool. The person set
three design rules: baton is one-shot, it guarantees the objective, and a new message fits in
between two tool calls.

## Decisions the person made

- Commands never start with `/`, which belongs to OpenCode: they start with `baton:`.
- Queue: codex, antigravity, claude. An agent gives way after 5 hours, or sooner when its limit runs out.
- Claude leaves the reserve state. Its turn ends at 5 hours or USD 50, whichever comes first.
- Claude leaves the queue for the rest of a goal after USD 150 in that goal. The others go on.
- A goal ends only when its check passes and every agent approves, waiting for an agent that is out
  of quota. An agent that cannot ever run (disabled, no key, over its goal cap) is left out.
- A goal is never given up: with no progress baton tells the person and carries on.

## Global constraints

- Without a goal, baton behaves as today, except for items that apply to every job (queue, turn
  limits, tool-boundary interrupts, inbox delivery, memory, masking).
- A `[baton]`-prefixed chat line reports every automatic decision.
- State that must survive a restart lives in `state.json` or the store directory, and an old
  `state.json` without the new keys loads fine.
- Nothing reads, or acts on, agent output as a command: commands come only from the person.
- Everything of OpenCode's database is read-only, and any failure reading it is silent and retried:
  its schema is not a promised interface.
- No private paths, names or keys in the repo (`tests/test_check_private.py`).
- `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` pass after each task.

## Task 1: Messages between tool calls

**Files:** `src/baton/gateway.py`, `src/baton/runner.py`, `src/baton/adapters/{base,antigravity,codex,claude_api}.py`, `src/baton/store.py`, tests, README.

Facts: OpenCode stores a message typed during a reply in table `session_inbox` (`id`, `session_id`,
`type`, `payload` JSON with `text`, `delivery` such as `steer`, `enqueued_seq`, `time_created`) and
POSTs it only after the reply stream ends, which for baton may be hours away.

**Queue delivery.** While a job runs, a daemon thread in `JobManager`, polling every 2 s through
`opencode_db()` in read-only mode (`file:...?mode=ro`, short timeout), reads rows of type `user`
for the running sessions. A row not seen before goes through the same path as `Job.add_note`, and
its id and a hash of its text are saved in `job.json` (`inbox_seen`), so a restart does not repeat it.
When OpenCode later POSTs the same text, `attach`/`submit` recognise the hash, accept the mark and
follow the job, with no second note. Baton never writes to that database.

**Large notes.** A note over `NOTE_INLINE_CHARS = 8000` goes to `.baton/notes/<UTC stamp>-<8 hex>.md`
in the workdir, and the note is its first 1000 characters plus a line naming the file and telling the
agent to read it whole. This also protects argv limits.

**Tool boundary.** New `ToolWatch` in `adapters/base.py`: `feed(line)` and a `busy` property.
`adapter.tool_watch()` returns one per round, or `None`. Real events:

- antigravity: `step_update` with a `tool_name`: `ACTIVE` opens and `DONE` closes, by `step_index`.
  Background: `manage_task` DONE output with `Status: RUNNING` adds `TaskId`; `Status: DONE`, other
  terminal statuses or `Action: kill` remove it; a `text_delta` matching
  `waiting for (\d+) background task` with a positive count sets busy until a `manage_task` removal
  or a new tool step.
- codex: `item.started` / `item.completed` with `item.type == "command_execution"`, by `item.id`.
- claude: `assistant` `tool_use` blocks open by `id`, `user` `tool_result` blocks close by `tool_use_id`.

`on_output` feeds the watch before render; a watch exception disables it for the round. In
`should_kill`, a taken interrupt no longer kills at once: it records the time, and kills when the
watch is not busy or `MESSAGE_WAIT_S = 300` has passed, whichever comes first (`killed =
interrupted = True` as today). `stop now` still kills at once. The chat line in `add_note` becomes
`[baton] message received: it reaches the agent after the current tool call`.

**Every conversation reads every note.** Notes are cleared after the first round that reads them, so a
resumed session misses a note another session consumed. Keep `note_log` in `state.json` (last 50, each
with a growing `seq`) and `note_seen` per session key. A round's `Notes from the person` holds every
entry after that key's `note_seen`, plus unread ones, and the key's mark advances when the round has
really run (`spawned and (worked or finished_its_turn)`). A fresh session, whose objective holds the
conversation, starts at the newest entry. A review has no key and gets only the current notes.

**Tests:** each watch against real-shape events; a parallel pair of tools stays busy until both end;
the interrupt waits for a tool to end and is capped by `MESSAGE_WAIT_S`; a row in a fake
`session_inbox` becomes one note, not repeated on restart or on the later POST; a missing table or
locked database is silent; a large note lands in a file.

## Task 2: Goal and commands

**Files:** new `src/baton/commands.py`, `src/baton/gateway.py`, `src/baton/store.py`, `src/baton/runner.py`, `src/baton/cli.py`, `src/baton/config.py`, tests, README.

`parse_command(text) -> Command | None`, matching on the start of the message, any case:
`baton: goal <text>` (the rest of the message, multi-line, required), `baton: goal off`,
`baton: next`, `baton: next now`. Anything else is an ordinary message, as is text that merely
contains these words.

Store: `set_goal(text)` (atomic write to `goal.md`), `goal()`, `clear_goal()`, `archive_goal()` (to
`goal.done.md`), `request_next(now)`, `take_next()`.

Gateway, for the newest person message:
- `goal <text>`: set it; start a job with `<text>` as the objective if none runs; else add it as a
  note with the path of Task 1. Chat: `[baton] goal set`.
- `goal off`: clear it. Chat: `[baton] goal cleared`. No note.
- `next`: `request_next(False)`; `next now`: also `request_interrupt()`, so it waits for the tool to
  end. With a running job, chat one line and follow the job; without one, answer one line and finish.
- A command's mark is accepted as usual, so a retry does nothing.

`build_prompt(pinned_goal=...)`: when a goal is set, the first part of every prompt, fresh or resumed:
`Goal, fixed by the person. It does not change until the person changes it:\n<text>`.

`goal.objective` for a goal file: new key `oneshot = true` in `GOAL_KEYS` sets the goal to the
objective at start. CLI: `baton goal <job> <text>`, `baton goal <job> --off`, `baton next <job>
[--now]`, using the `_resolve` of `baton say`.

`next` is only stored here; Task 3 makes it rotate.

## Task 3: The queue

**Files:** `src/baton/scheduler.py`, `src/baton/config.py`, `src/baton/runner.py`, `src/baton/adapters/{base,claude_api}.py`, tests, README (the rotation section is rewritten).

Config, per agent: `turn_max_s` (default 18000). `[agents.claude_api]`: `reserve` (default false),
`turn_budget_usd` (50), `goal_budget_usd` (150). The monthly ledger cap stays above all of this.

`adapter.reserve` (false for codex and antigravity; `cfg.reserve` for claude). Every place that means
"waits for the others" moves from `metered` to `reserve`: `_plan_names`, `_candidates`, `avoid` in
`pick`, `_judge_preempt`, and `PreemptionWatch` in the runner. With `reserve = true` behaviour is
exactly today's. `metered` stays for budget meaning only.

`RoundResult.cost_usd: float = 0.0`, set by claude_api from `stream.cost`. `round_end` carries
`cost_usd`. A failed resume's first attempt counts too.

Scheduler, persisted: `turn` (`{"agent", "started", "spent"}`) and `goal_spent` per agent, plus
`dropped`. New:
- `note_cost(agent, usd)`.
- `turn_over(agent)`: elapsed since the turn started >= `turn_max_s`, or spent >= `turn_budget_usd`
  when that agent has one.
- `rotate(reason)`: `current` moves to the next agent in `order` that is not dropped, a new turn starts,
  and it returns the name. Unavailable agents are skipped by `pick` as today.
- `pick` starting a different agent than the turn's starts a new turn (limit ran out).
- `drop(agent)`: out of `_candidates` for this goal; a new goal or `goal off` clears `dropped` and
  `goal_spent`. `goal_budget_usd` only applies while a goal is set.

Runner, after `round_end`: take `next`, then `turn_over`; either rotates once and emits `rotation`
with a reason (`asked by the person`, `5 hours`, `USD 50`), rendered as
`[baton] rotation: a -> b (<reason>)`. Crossing the goal budget drops the agent and alerts. Rotation
happens only between rounds. Between rounds `next` is also taken when the job is waiting.

## Task 4: The objective guaranteed

**Files:** `src/baton/phases.py`, `src/baton/runner.py`, `src/baton/gateway.py`, tests, README.

Applies only while a goal is set.

**The check.** `goal.verify_command`, when there is one. Otherwise `.baton/VERIFY.sh`: `phase_prompt`
for PLAN tells the agent to write it, exiting 0 only when the goal is met as the person would
experience it, in the real setup, with no rounding, and that measuring a stand-in fails the check.
With neither, the gate fails with `no check`, and the run stays in PLAN.

**Lock.** At the end of each completed PLAN round the script's hash is saved (`check_lock`) and its
first version kept in the store (`verify.first.sh`). A gate that finds another hash fails with
`the check changed outside plan` and alerts. The agent cannot weaken it silently: a consensus prompt
includes the unified diff against the first version, and tells reviewers to reject a weaker check.

**End.** While a goal is set `finish` is reached only through consensus: `finish("verify passed")`,
`finish("round completed")` and the no-verify return in `run_round` become: a cycle's end runs the gate,
and a passed gate starts consensus. A failed gate goes to PLAN as today.

**Consensus.** New phase `CONSENSUS` (tier strong, fresh session, no `avoid`). Persisted:
`consensus = {"pending": [...]}`, starting with every agent in `order` that can run: enabled, not
dropped, with a key. `step()` runs one pending agent at a time, through `Scheduler.pick_for(name)`,
which reads that agent without moving `current`; if it is out of quota the job waits (event `wait`,
reason `waiting for <agent> to approve`, using `next_wake`).

A consensus round may not change the tree (`tree_fingerprint`, as a review), reruns the check itself,
inspects real evidence, and writes `REVIEW.md` with `APPROVED` or `CHANGES` first. Then:
- Approved: leaves `pending`. Empty `pending`: `finish("goal achieved")`, `archive_goal()`, chat
  `[baton] goal achieved: check passed, approved by a, b, c`.
- `CHANGES` or a changed tree: consensus ends, the review text becomes a carried note, a task is added
  to `PLAN.md`, the phase goes to BUILD. Chat: `[baton] <agent> rejected: back to build`.
- A round that did not complete: stays pending and is retried.

A person's message during consensus ends it and goes to BUILD.

**No giving up.** `failed_cycles` counts cycles that end with a failed gate or a rejection. Every 3,
emit an `alert`: `no progress in N cycles: still working`. It never stops.

## Task 5: Memory

**Files:** `src/baton/memory.py`, tests.

- `_block`: for each hit call `read-page`, strip the frontmatter, and use the page's text up to
  `HIT_MAX_CHARS`, sharing `recall_chars` across the hits, instead of the search snippet. A failed read
  falls back to the snippet. Stay within `CALL_BUDGET_S`.
- Leave out round pages of the current job (`rounds/<job>/...`): the handoff holds them. Ask search for
  enough hits to still fill `MAX_HITS`. Order: lessons and context, then the rest.
- `lessons_of`: besides `## Lessons`, collect `## Lessons ...` and `### Lessons ...` headings, and
  list items whose bold text starts with `Lessons` (`- **Lessons:**`, `- **Lessons — x:**`) with their
  indented children, in file order.
- Emit a `memory` event `recorded` when a round's page is written.

## Task 6: Upkeep

**Files:** `src/baton/proc.py`, `src/baton/runner.py`, `src/baton/gateway.py`, `src/baton/store.py`, `src/baton/cli.py`, tests, new `tests/fixtures/`.

- **Masking.** `mask_secrets(text)`: keeps the name and replaces the value with `[masked]` for
  `Bearer <v>`, `<token|api_key|api-key|secret|password|passwd>[=:]<v>`, and masks `sk-...`,
  `gh[pousr]_...`, `AKIA...`, `xox.-...` tokens. Applied in `on_output` to rendered text, before `buf`
  and the event, for every adapter.
- **Real log test.** A sanitised `tests/fixtures/agy_round.jsonl` with the shapes of a real round (init
  at top level, split deltas, tool ACTIVE and DONE, `manage_task`, usage, result), and a test that sends it
  through the adapter and `render_event`.
- **Snapshots.** Add `__pycache__/` and `*.pyc` to `.git/info/exclude` beside `.baton/`.
- **Native restart.** `baton restart` writes `<state base>/restart`. While it exists, a runner holds
  before `pick` and while waiting, emitting `held` once. A gateway thread sees all running jobs held,
  removes the file and sends itself SIGTERM; launchd restarts it and `recover` resumes the jobs. Runners
  outside the gateway ignore it. Tested with fakes, with no signal to a real service.
