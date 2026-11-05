# baton: design

## Intent

A single long coding goal should keep moving for days across three agents, in this order: Codex (GPT-6.1 Sol), Antigravity (Gemini 4 Argon) and Claude Opus 5.5 through an organization API key, all at medium effort,, without a person restarting anything and without ever spending pay-as-you-go credits.

The real failure this exists to prevent: at 100% of its included usage, the Codex CLI keeps working and silently debits account credits. No error is raised and no 429 reaches the client. Any design that waits for a limit error loses money first. Limits must be read and acted on before they are reached.

## Goals

1. Run one goal to completion: stay on the current agent until its limit, pass to the next, and when all are exhausted resume with whichever returns first.
2. Finish only when a verification command exits 0. An agent saying "done" means nothing.
3. Never spend credits: stop an agent before its included usage ends, and kill it if a credit balance moves.
4. Never give up: when every agent is exhausted, wait for the earliest reset and continue.
5. Use it from OpenCode as one model, `baton/auto`, keeping history and context there.

## Non-goals

- Claude subscriptions. Claude only runs with a Console API key.
- Reimplementing a coding agent. Every agent runs through its vendor's own CLI.
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
- **agent**: a vendor CLI behind an adapter: `codex`, `antigravity`, `claude_api`.
- **round**: one agent process, fresh or resuming that vendor's conversation, working until it exits or is stopped.
- **gate**: the verification command, run before the first round and after every round.

## Behaviour

### Main loop

```
gate passes?                 -> finish, exit 0
stop file present?           -> exit 0, goal unfinished
keep current agent if available, else next available in rotation
none available               -> sleep until earliest known reset, wake early for stop
run one round
snapshot uncommitted work
run gate
```

Agents come in two kinds. Plan agents (`codex`, `antigravity`) have included usage that comes back at a reset; a metered agent (`claude_api`) is paid by use and has no reset that would ever stop it, so it would run until the credit is gone. The rotation therefore treats a metered agent as a fallback, wherever it appears in `order`:

- The current plan agent keeps working until it becomes unavailable, then the rotation moves to the next plan agent in `order`, wrapping around. A plan agent that comes back does not preempt another plan agent that is working.
- A metered agent runs only while no plan agent is available.
- While a metered agent runs, the plan agents are read (without a model call) from their reset time onward. As soon as one reads available, the metered round is killed and that agent takes over. If both are available, the first in `order` goes first. This preemption is not a failure and gives the metered agent no cooldown.
- A plan agent that reads available but whose round, right after such a preemption, fails (anything but `completed` or `timeout`) within 300 s is blocked from preempting: it is no longer read while a metered agent runs, so one bad agent cannot cost a metered round per attempt. The block is kept in `state.json`. It ends when a round of that agent completes, times out or lasts at least 300 s. At a round boundary the agent is still picked as usual.
- With no agent available, the wait ends at the earliest known reset.

A preempted Claude round has no `result` event, so its whole reservation stays counted. That overcounts and never overspends; `round_budget_usd` defaults to 5 USD to keep it small.

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

`resetsAt` of the saturated window, or the latest one if several, sets the cooldown. When `ordinaryUsageAllowed` is false and no window is at the threshold, the cause is unknown and the agent is read again after 5 min.

Command:

```
codex exec --json --skip-git-repo-check -m <model> -c model_reasoning_effort=<effort> \
  --dangerously-bypass-approvals-and-sandbox -C <workdir> -
```

The prompt goes on stdin. A goal can set `full_access = false` to drop the bypass flag and use `--sandbox workspace-write`.

Interactive Codex sessions outside baton share the same limits and credits. baton cannot stop them, but a balance drop they cause still stops baton's round.

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

Relevant rows are those whose first column is `Gemini Models` or starts with `Gemini`. Rows for `Claude and GPT models` are ignored. Used percent is `100 - remaining`. Both `Gemini Models` rows, Five Hour and Weekly, and a parseable credit balance are required, otherwise the agent is unavailable and retried after 5 min.

Start a round only if every relevant row has used percent below `threshold_pct` (default 90). While a round runs, read both every `poll_s` (default 30) and apply the same rules as Codex: threshold reached is `limit`, credits below the balance read just before the round is `credit_guard`, two consecutive failed reads is `credit_guard`. A saturated row's reset sets the cooldown, the latest when several saturate.

The final stream line is `{"event":"result","result":{"status":"SUCCESS","response":"...","error":"..."}}`. Text deltas appear in `step_update.text_delta`. A status other than `SUCCESS` is classified by pattern on error text and output: `RESOURCE_EXHAUSTED|quota|rate.?limit|429` is `limit`; `authentication|sign in|log in` is `auth`; otherwise `transient`.

### Claude API adapter

Runs the Claude Code CLI against a Console API key, billed to the organization's monthly promotional credit. No subscription login is involved.

```
claude -p --output-format stream-json --verbose --bare --model <model> --effort <effort> \
  --dangerously-skip-permissions --max-budget-usd <round cap>
```

Prompt on stdin. The key is read from `key_file` at spawn and passed only in that process's environment as `ANTHROPIC_API_KEY`. It is never logged, stored in state, or passed to other agents. The file must be a regular file with mode 600.

**There is no API that returns the credit balance.** The budget is therefore baton's own: `monthly_budget_usd` (default 200) minus `reserve_pct` (default 10) gives a spend ceiling of 180.

`monthly_budget_usd`, `reserve_pct` and `budget_reset_day` are global: they live only in `~/.config/baton/defaults.toml`, which also holds `[agents.*]` overrides applied under every goal, and are stored in the ledger. A mismatch makes the agent unavailable.

Spend tracking is a ledger shared across all goals at `~/.local/state/baton/claude-spend.json`, updated under a lock:

- Spend comes from `total_cost_usd` in the `result` event. It is a local estimate, so `reserve_pct` also absorbs its error.
- A period starts on `budget_reset_day` (default 1) at 00:00 UTC.
- Before a round, atomically reserve `round_budget_usd` (default 5). If spent plus all live reservations plus the round would pass the ceiling, shrink the round to what is left; below 5 USD the agent is unavailable until the next period.
- After a round, replace the reservation with the actual cost. If a round ends with no `result` event, the full reservation stays counted. Crashes therefore overcount and never undercount.
- An unreadable or corrupt ledger means unavailable. Never recreate it silently.

If `admin_key_file` is set, also read `GET /v1/organizations/cost_report` for the current period, and treat spend as the larger of that and the ledger. Reporting may lag, so it never lowers the ledger. A failed read when the file is set means unavailable.

Stream checks:

- `{"type":"system","subtype":"init","apiKeySource":...}`: anything other than `ANTHROPIC_API_KEY` stops the round with `credit_guard`.
- Sum `total_cost_usd` is only final at `result`; `--max-budget-usd` is the in-round stop.
- Output or exit text matching credit balance, billing or spend limit is `limit` until the next period. HTTP 429 and `overloaded` are `limit` with the no-known-reset backoff.

Availability with `enabled = true` and no key file: unavailable, with a clear reason, retried after 1 hour. This never blocks the other agents.

`--bare` keeps the CLI from falling back to the subscription login. It also skips project instructions, which the prompt's handoff replaces.

The account owner must, in the Console: turn auto-reload off and hold no purchased credits, since otherwise spending continues after the promotional credit ends; and put the key in a dedicated workspace with a monthly spend limit at the ceiling. `baton doctor` prints this reminder, since neither can be checked with the key.

### Environment

Every agent process has `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `GOOGLE_API_KEY`, plus the Codex key variables, removed from its environment, so a vendor CLI cannot fall back to paid API access. The Claude adapter then adds its own key back for its process only.

### Process control

Agents start in their own session. Stopping sends SIGTERM to the process group, then SIGKILL after 10 s. The active group ID is stored in state; on startup a recorded group that is still alive is killed first. A lock in the state directory enforces one runner per goal.

On macOS the runner holds `caffeinate -is -w <pid>`.

### Context between rounds

Context has three layers:

1. **Shared by every agent:** the repository, its commits, `.baton/HANDOFF.md`, the last gate output, and the rendered tail of the previous round (8000 chars). The tail covers what an agent had not yet written down when it was stopped.
2. **Private to each vendor:** when an agent's turn comes again, baton resumes that vendor's own conversation, so it keeps its full history. The prompt then only adds what changed since its last round: other agents' commits, the handoff, the gate output and the tail.
3. **Everything else stays local:** raw logs in `rounds/` are for people, not prompts.

Session ids come from the stream: `thread_id` in Codex `thread.started`, `conversation_id` in the Antigravity `init` event, `session_id` in the Claude `init` event. They are stored per agent in `state.json`.

Resume commands: `codex exec resume <id>` with the same flags, `agy --conversation <id>`, `claude -p --resume <id>`. If a resume fails for any reason, or the working directory changed, clear the id and run the same round fresh with the full prompt. A failed resume never counts against the agent.

### Prompt and handoff

A fresh round's prompt contains: the objective, the verify command, the tail of the last gate output (4000 chars), the tail of the previous round, unread notes from the person, the handoff file, and these rules:

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
- `interrupt`: presence ends the current round now. The note is delivered at once to the next round, which resumes the same agent when it is still available. The file is consumed when the round ends.
- `stop`: presence requests a stop after the current round. Second form, `stop now`, kills the round.

### Goal file

```toml
name = "example"
workdir = "/path/to/project"
objective = "Make the build pass on all platforms."
order = ["codex", "antigravity", "claude_api"]
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

[agents.claude_api]
enabled = true
model = "claude-opus-5-5"
effort = "medium"
monthly_budget_usd = 200
reserve_pct = 10
round_budget_usd = 5
budget_reset_day = 1
key_file = "~/.config/baton/anthropic.key"
admin_key_file = ""

```

Every value except `name`, `workdir` and `objective` has the default shown. Without `verify.command` the goal is finished when a round ends as `completed`. Binary paths are resolved from `PATH`, overridable with `bin`, since tmux and launchd may not load shell aliases.

### Commands

- `baton doctor [goal]`: binaries, logins, stray API keys, live limits, Claude spend against its ceiling. Spends nothing. Non-zero exit on a blocking problem.
- `baton run <goal.toml>`: one goal in the foreground, without OpenCode.
- `baton serve [--port 18777]`: the OpenCode gateway.
- `baton status <goal.toml>`: one-shot text.
- `baton say <goal.toml> [--now] "note"`: adds to the inbox. `--now` also interrupts the current round.
- `baton stop <goal.toml> [--now]`: requests a stop.
- `baton opencode-config [--port 18777]`: prints the provider JSON for `opencode.json`.
- `baton jobs`: lists jobs; `status`, `say` and `stop` also accept a job name.
- `baton install-service [--port 18777]`: writes the launchd plist and loads it.

### OpenCode gateway

OpenCode sees one OpenAI-compatible provider. A spike with a stub server proved that `@ai-sdk/openai-compatible` streams from it, that OpenCode sends the full history each request, and that each request carries `x-opencode-session`. The spike only covered gaps of seconds.

The server binds `127.0.0.1` only and uses the standard library's threading HTTP server.

- `GET /v1/models` returns one model, `auto`.
- `POST /v1/chat/completions` with `stream: true` streams `chat.completion.chunk` SSE.

Request handling:

- **Title requests** have no `tools` key. Answer at once with up to 6 words of the first user message, without calling an agent, so they spend nothing.
- **No job for the session:** create one. The job's name is `session-<id>`, its state lives in the usual state directory, and the workdir comes from `select directory from session_v2 where id = ?`, read-only, on OpenCode's `opencode.db` (`~/.local/share/opencode/opencode.db`, path overridable with `BATON_OPENCODE_DB`). If that yields nothing, respond with a clear error message. Do not guess a directory.
- **Goal:** if the workdir holds `baton.toml`, its `[verify]` and agent settings apply and the objective is the user's message. Otherwise there is no gate, and the job finishes when a round completes.
- **Job already running for the session:** the new message becomes a note with interrupt, and the stream attaches to the job.
- **Job finished for the session:** a new one starts, reusing the stored vendor session ids.

On startup `baton serve` kills every recorded agent group that is verifiably ours and still alive, then resumes every job that had not finished. `job.json` in the job's state directory stores the session, workdir, objective and conversation. A message from a person always gets at least one round, even if the gate already passes. A repeated POST carrying no new user message attaches to the stream without interrupting. `baton.toml` may not set `bin`, `key_file`, `admin_key_file` or `full_access`. `threshold_pct` above 90 is rejected everywhere, and an `antigravity.model` must start with `gemini-`. `baton install-service` writes a launchd plist with absolute paths, KeepAlive, a PATH built from the resolved agent binaries and an ExitTimeOut above the shutdown wait.

A job runs in its own thread inside `baton serve` through `run_goal`, independent of the HTTP connection. A dropped connection never stops it. Stream output: rendered agent text, one line on each rotation, wait or gate result, and a final summary. Send an empty-content delta every 15 s so long waits do not trip client timeouts. The `tools` field is ignored, since each CLI uses its own tools. `finish_reason: "stop"` is sent only when the job finishes, or `baton stop` is honoured.

History: when a job starts with no vendor session id, the prompt includes the OpenCode conversation so far, up to the last 24000 chars. With a vendor session, only the new message goes in.

## Constraints

- Python 3.12, managed with uv. No runtime dependencies.
- No secrets are read, printed or logged by baton itself. Credential files are never opened.
- Tests never call a real vendor. Fake CLIs and fake limit readers stand in.

## Risks to state plainly

- An interrupted Claude round has no final cost, so its full reservation stays counted until the period rolls. Each interrupt can cost up to `round_budget_usd` of the ceiling.

- Claude spend is an estimate. The hard stops are the credit running out, auto-reload being off, and the workspace spend limit, all configured in the Console.

- A round can cross from threshold to 100% between polls. The 10% margin exists for this, and it cannot be mathematically zero. If the account's own setting for credits is available, it remains the only hard stop.
- A response held open for days through OpenCode is untested beyond seconds. The job does not depend on it: `baton status` and reattaching still work.
- Antigravity's output when quota actually runs out has not been observed; the limit reads above are expected to stop it first.
