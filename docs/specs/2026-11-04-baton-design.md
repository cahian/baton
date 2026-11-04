# baton: design

## Intent

A single long coding goal should keep moving for days across several subscription-based coding agents, without a person restarting anything and without ever spending pay-as-you-go credits.

The real failure this exists to prevent: at 100% of its included usage, the Codex CLI keeps working and silently debits account credits. No error is raised and no 429 reaches the client. Any design that waits for a limit error loses money first. Limits must be read and acted on before they are reached.

## Goals

1. Run one goal to completion, rotating in a fixed priority order.
2. Finish only when a verification command exits 0. An agent saying "done" means nothing.
3. Never spend credits: stop an agent before its included usage ends, and kill it if a credit balance moves.
4. Never give up: when every agent is exhausted, wait for the earliest reset and continue.
5. One screen to watch it and talk to it.

## Non-goals

- Routing a subscription through third-party clients. Claude subscriptions are not permitted in third-party tools, so every agent runs through its vendor's own CLI.
- Merging conversation history between vendors. Continuity comes from the repository and a handoff file.
- Pushing code, opening PRs, or touching anything beyond the working directory on the agent's behalf. Goals may instruct agents to do so.

## Anti-goals

Any of these is a failure even if the goal finishes:

- A credit is debited because baton kept an agent running after its included usage was gone.
- A safety check that cannot read its data lets the agent run anyway. Unknown means stop.
- baton exits because of a limit, a transient error, a crash in an agent, or bad network.
- The goal is reported finished without the verification command exiting 0.
- Personal data, hostnames, account IDs or secrets enter this repository.

## Vocabulary

- **goal**: a TOML file describing the objective, working directory and verification command.
- **agent**: a vendor CLI behind an adapter: `codex`, `antigravity`, `claude`.
- **round**: one agent process, started fresh, working until it exits or is stopped.
- **gate**: the verification command, run before the first round and after every round.

## Behaviour

### Main loop

```
gate passes?                 -> finish, exit 0
stop file present?           -> exit 0, goal unfinished
pick first available agent in goal order
none available               -> sleep until earliest known reset, wake early for stop
run one round
snapshot uncommitted work
run gate
```

After each round the first agent in the order is reconsidered, so the highest priority agent returns as soon as its limit resets. A round in progress is not preempted.

Exceptions in the loop are logged, followed by a 60 s sleep, then the loop continues. Only a passing gate, a stop request or a termination signal ends the process.

### Round outcomes

| Outcome | Meaning | Effect |
|---|---|---|
| `completed` | agent exited by itself | next round |
| `limit` | stopped at threshold or vendor refused | cooldown until reset |
| `credit_guard` | a paid balance moved or overage became active | cooldown until reset, or 30 min if no reset is known; alert event |
| `auth` | login missing or expired | cooldown 30 min; alert event |
| `transient` | any other failure | exponential backoff, 1 min doubling to 30 min, reset by the next `completed` round |
| `timeout` | exceeded `round_max_s` or `idle_s` | treated as `completed`, the work is kept |

A limit with no known reset uses backoff of 15 min doubling to 60 min. Each attempt is a real round, so it only happens after the wait.

### Codex adapter

Limits come from `codex app-server` over stdio JSON-RPC: `initialize`, the `initialized` notification, then `account/rateLimits/read`. This makes no model call and spends nothing. Spawn a fresh process per read, with a 15 s timeout.

Relevant response fields, under `result`:

```json
{
  "ordinaryUsageAllowed": true,
  "rateLimits": {
    "primary":   {"usedPercent": 12, "windowDurationMins": 300,   "resetsAt": 1700000000},
    "secondary": {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": 1700000000},
    "credits": {"hasCredits": true, "unlimited": false, "balance": "1000.5000000000"},
    "rateLimitReachedType": null,
    "planType": "plus"
  }
}
```

Start a round only if all hold, otherwise the agent is unavailable:

- `ordinaryUsageAllowed` is exactly `true`
- `rateLimitReachedType` is null
- `primary` exists and every present window has `usedPercent` below `threshold_pct` (default 90)

`secondary` may be null on some plans. A missing `primary`, a failed read or an unparseable balance means unavailable, retried after 5 min.

While a round runs, read limits every `poll_s` (default 15). Stop the round, with `limit`, when any start condition fails. Stop it, with `credit_guard`, as soon as the balance is below the balance read immediately before the round. Compare with `Decimal`. Two consecutive failed reads stop the round with `credit_guard`: being blind is treated as danger.

`resetsAt` of the saturated window, or the latest one if several, sets the cooldown.

Command:

```
codex exec --json --skip-git-repo-check -m <model> -c model_reasoning_effort=<effort> \
  --dangerously-bypass-approvals-and-sandbox -C <workdir> -
```

The prompt goes on stdin. A goal can set `full_access = false` to drop the bypass flag and use `--sandbox workspace-write`.

Interactive Codex sessions outside baton share the same limits and credits. baton cannot stop them, but a balance drop they cause still stops baton's round.

### Claude adapter

```
claude -p --output-format stream-json --verbose --model <model> --effort <effort> \
  --dangerously-skip-permissions
```

Prompt on stdin. Parsed stream events:

- `{"type":"system","subtype":"init","apiKeySource":...}`: anything other than `"none"` stops the round with `credit_guard`, because it would bill an API key.
- `{"type":"rate_limit_event","rate_limit_info":{...}}` with `status`, `resetsAt`, `rateLimitType`, `isUsingOverage`, `overageStatus`, and `unifiedWindows.<name>.{utilization,resetsAt,status}`.

Stop with `credit_guard` when `isUsingOverage` is true. Stop with `limit` when `status` is `rejected`, or when any window `utilization` reaches `threshold_pct / 100` (default 95). `status` of `allowed` or `allowed_warning` under the threshold keeps going. Cooldown comes from the relevant `resetsAt`.

There is no read-only limit query, so availability before a round is only the local cooldown. If the stream never produced a `rate_limit_event`, the round is not blocked: the vendor refuses overage on accounts that have it disabled, and the `init` check covers API billing. Exit text matching `limit|usage` with a non-zero exit is classified `limit`.

### Antigravity adapter

```
agy -p <prompt> --output-format stream-json --model <model> --dangerously-skip-permissions
```

Run with the working directory set and stdin closed. The effort is part of the model id, for example `gemini-4-argon-medium`.

Limits come from two slash commands that make no model call. Run each with `agy -p <command> --output-format text`, stdin closed, 30 s timeout.

`/usage` prints tab-separated rows of group or model, label, percent remaining, and ISO 8601 reset:

```
Gemini Models<TAB>Weekly Limit Remaining<TAB>100%<TAB>2026-11-11T17:56:19Z
Gemini Models<TAB>Five Hour Limit Remaining<TAB>100%<TAB>2026-11-04T22:56:19Z
Claude and GPT models<TAB>Weekly Limit Remaining<TAB>100%<TAB>2026-11-11T17:56:19Z
Gemini 4 Argon<TAB>Gemini 4 Argon Weekly Limit<TAB>99%<TAB>2026-11-11T17:56:01Z
```

`/credits` prints `Remaining credits<TAB>0`.

Relevant rows are those whose first column is `Gemini Models` or starts with `Gemini`. Rows for `Claude and GPT models` are ignored. Used percent is `100 - remaining`. At least one `Gemini Models` row and a parseable credit balance are required, otherwise the agent is unavailable and retried after 5 min.

Start a round only if every relevant row has used percent below `threshold_pct` (default 90). While a round runs, read both every `poll_s` (default 30) and apply the same rules as Codex: threshold reached is `limit`, credits below the balance read just before the round is `credit_guard`, two consecutive failed reads is `credit_guard`. A saturated row's reset sets the cooldown, the latest when several saturate.

The final stream line is `{"event":"result","result":{"status":"SUCCESS","response":"...","error":"..."}}`. Text deltas appear in `step_update.text_delta`. A status other than `SUCCESS` is classified by pattern on error text and output: `RESOURCE_EXHAUSTED|quota|rate.?limit|429` is `limit`; `authentication|sign in|log in` is `auth`; otherwise `transient`.

### Environment

Every agent process has `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` and `ANTHROPIC_AUTH_TOKEN` removed from its environment, so a vendor CLI cannot fall back to paid API access.

### Process control

Agents start in their own session. Stopping sends SIGTERM to the process group, then SIGKILL after 10 s. The active group ID is stored in state; on startup a recorded group that is still alive is killed first. A lock in the state directory enforces one runner per goal.

On macOS the runner holds `caffeinate -is -w <pid>`.

### Prompt and handoff

Each round's prompt contains: the objective, the verify command, the tail of the last gate output (4000 chars), unread notes from the person, the handoff file, and these rules:

- You are one shift of a relay. Another agent may continue after you at any moment.
- Commit working progress often, in small steps.
- Keep `.baton/HANDOFF.md` current: what was tried, what was learned, what comes next.
- Do not declare success. Only the verify command decides.

`.baton/` lives in the working directory and is added to `.git/info/exclude`.

After each round, if the working directory is a git repository with uncommitted changes, baton commits them locally with `git add -A` and message `baton: snapshot round <n> (<agent>)`, using `--no-verify`. Snapshots protect work from a hard stop. baton never pushes. `snapshot = false` turns this off.

### Gate

The verify command runs through the shell in the working directory with `verify.timeout_s`. Exit 0 finishes. Anything else, including a timeout, means not done, and the output goes to the next round. A run where the gate cannot be executed never counts as passing.

### State

`~/.local/state/baton/<goal name>/`:

- `events.jsonl`: every event, one JSON object per line, with `ts`, `kind` and fields. Rotates at 50 MB.
- `state.json`: round number, per-agent cooldown and backoff, active group. Written atomically.
- `rounds/<n>-<agent>.log`: raw agent output.
- `inbox.md`: notes from the person, consumed into the next prompt.
- `stop`: presence requests a stop after the current round. Second form, `stop now`, kills the round.

### Goal file

```toml
name = "example"
workdir = "/path/to/project"
objective = "Make the build pass on all platforms."
order = ["codex", "antigravity", "claude"]
round_max_s = 3600
idle_s = 1200
snapshot = true

[verify]
command = "./check.sh"
timeout_s = 3600

[agents.codex]
enabled = true
model = "gpt-6.1-sol"
effort = "medium"
threshold_pct = 90
poll_s = 15

[agents.antigravity]
enabled = true
model = "gemini-4-argon-medium"
threshold_pct = 90
poll_s = 30

[agents.claude]
enabled = true
model = "sonnet"
effort = "high"
threshold_pct = 95
```

Every value except `name`, `workdir`, `objective` and `verify.command` has the default shown. Binary paths are resolved from `PATH`, overridable with `bin`, since tmux and launchd may not load shell aliases.

### Commands

- `baton doctor [goal]`: binaries, logins, stray API keys, live Codex limits, and any reason an agent would be skipped. Spends nothing. Non-zero exit on a blocking problem.
- `baton run <goal.toml>`: the runner, in the foreground.
- `baton panel <goal.toml>`: the interface.
- `baton up <goal.toml>`: runner and panel together in a tmux session named `baton-<name>`, reattached if it exists.
- `baton status <goal.toml>`: one-shot text.
- `baton say <goal.toml> "note"`: adds to the inbox.
- `baton stop <goal.toml> [--now]`: requests a stop.

### Panel

A Textual app that only reads state and events and writes to the inbox and stop file. Quitting it leaves the runner untouched. It shows:

- one card per agent: state, usage percentages, countdown to reset
- current round, elapsed time, last gate result
- a live feed of normalized agent output
- an input line that sends notes to the inbox

## Constraints

- Python 3.12, managed with uv. Textual is the only runtime dependency; the runner must work if Textual is not importable.
- No secrets are read, printed or logged by baton itself. Credential files are never opened.
- Tests never call a real vendor. Fake CLIs and fake limit readers stand in.

## Risks to state plainly

- A round can cross from threshold to 100% between polls. The 10% margin exists for this, and it cannot be mathematically zero. If the account's own setting for credits is available, it remains the only hard stop.
- A long continuous loop may fall outside Anthropic's "ordinary, individual usage". The page does not define the limit.
- Antigravity's output when quota actually runs out has not been observed; the limit reads above are expected to stop it first.
