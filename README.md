# baton

baton keeps one long coding goal moving for days by passing it between three coding agents,
stopping each one before it can spend money it should not.

You give it a goal (an objective, a working directory and a verification command). It runs one
agent until that agent nears its limit, hands the work to the next, and when every agent is out,
sleeps until the earliest reset and carries on. It finishes only when the verification command
exits 0; an agent saying "done" means nothing.

Every agent runs through its vendor's own CLI. baton reimplements no agent. Context moves between
them through the repository, its commits and a handoff file, and each vendor's own conversation
is resumed when its turn comes again.

## The rotation

The default order, all at medium effort:

1. **Claude Opus 5.5**, through a Console API key (never a subscription login).
2. **Gemini 4 Argon**, through Antigravity (`agy`).
3. **GPT-6.1 Sol**, through Codex.

The first agent in `order` starts. The current agent keeps working until it is unavailable, then
the rotation moves on. When the last agent runs out it wraps to the first one that is available.
When none is, baton waits for the earliest known reset. An agent that comes back does not preempt
the one working. Change the order, or disable an agent, in the goal file.

## How each agent is stopped before it costs money

The failure this exists to prevent: at 100% of its included usage, the Codex CLI keeps working
and silently debits account credits. No error is raised. A design that waits for a limit error
has already paid, so baton reads the limits and acts before they are reached.

| Agent | What is read | Stop condition |
|---|---|---|
| Codex | `codex app-server` rate limits and credit balance. No model call, nothing spent. | 90% used in any window, or the credit balance drops |
| Antigravity | the `/usage` and `/credits` slash commands. No model call, nothing spent. | 90% used in any Gemini row, or the credit balance drops |
| Claude | baton's own spend ledger (there is no API that returns the balance) | a spend ceiling of 180 USD by default, and a per-round cap |

Rules that apply to all of them:

- Limits are read before a round and every few seconds during it (15 s for Codex, 30 s for
  Antigravity by default). Reaching the threshold stops the round.
- A balance that moves between the read before the round and any read during it kills the round
  at once (`credit_guard`). Balances are compared as exact decimals.
- Unknown means stop. A read that fails or cannot be parsed never lets an agent run, and two
  failed reads in a row during a round stop it.
- Every agent process has the vendor API-key variables removed from its environment, so a CLI
  cannot fall back to paid API access. The Claude adapter adds its own key back for its process
  only.

### The Claude ledger

Claude is billed to the organization's promotional credit, and no API reports what is left, so the
budget is baton's own:

- `monthly_budget_usd` (default 200) minus `reserve_pct` (default 10) is the spend ceiling, 180
  USD. The reserve also absorbs the error of the cost estimate.
- Spend is summed from `total_cost_usd` in each round's result, in one ledger shared by every
  goal (`~/.local/state/baton/claude-spend.json`), under a lock. A period starts on
  `budget_reset_day` at 00:00 UTC.
- Before a round, baton reserves `round_budget_usd` (default 10), passed to Claude as
  `--max-budget-usd`. If the ceiling would be passed, the round shrinks to what is left, and below
  5 USD the agent is unavailable until the next period.
- A round that ends without a result keeps its whole reservation counted. Crashes overcount and
  never undercount. A corrupt ledger makes the agent unavailable; it is never recreated silently.
- Optionally, `admin_key_file` points to an Admin API key. baton then also reads the Console cost
  report and uses the larger of that and the ledger. It never lowers the ledger, and a failed read
  makes the agent unavailable.
- With no key file, Claude is unavailable with a clear reason and the other agents carry on.

## Honest limits

- **There is a poll gap.** A round can cross from the threshold to 100% between two polls. The 10%
  margin exists for this, and it cannot be made mathematically zero. The account's own setting for
  credits, where it exists, remains the only hard stop.
- **Claude spend is an estimate.** It is computed locally from what the CLI reports. The hard stops
  are in the Console, not in baton.
- **Some Console settings must be right, and baton cannot check them with the key.** Turn
  auto-reload off and hold no purchased credits, since otherwise spending continues after the
  promotional credit ends. Put the key in a dedicated workspace with a monthly spend limit at the
  ceiling. `baton doctor` prints this reminder.
- **Interactive sessions outside baton are not protected.** Codex and Antigravity share one set of
  limits and credits between baton and anything else you run. baton cannot stop your own session,
  but a balance drop it causes still stops baton's round.
- **Antigravity's output when its quota actually runs out has not been observed.** The limit reads
  are expected to stop it first.
- **A response held open for days through OpenCode is untested** beyond seconds. The job does not
  depend on it: `baton status` and reattaching still work.

## Install

```
uv tool install .
baton doctor
```

Python 3.12 or newer. There are no runtime dependencies. You also need the CLIs you want to use on
your `PATH` and logged in: `codex`, `agy` and `claude`. Binary paths can be overridden per agent
with `bin`, since tmux and launchd may not load your shell aliases.

`baton doctor [goal]` checks binaries, logins, stray API-key variables, live limits and Claude's
spend against its ceiling. It spends nothing and exits non-zero on a blocking problem.

## Configuration

### `~/.config/baton/defaults.toml`

Settings applied under every goal. The Claude budget (`monthly_budget_usd`, `reserve_pct`,
`budget_reset_day`) can only be set here, because the ledger it describes is shared; a goal file
that sets them is refused. A full example, also in [`examples/defaults.toml`](examples/defaults.toml):

```toml
[agents.claude_api]
enabled = true
model = "claude-opus-5-5"
effort = "medium"
monthly_budget_usd = 200   # what the organization holds
reserve_pct = 10           # kept back; the spend ceiling is 200 - 10% = 180
budget_reset_day = 1       # day of the month the period starts, 00:00 UTC
round_budget_usd = 10      # most one round may spend
key_file = "~/.config/baton/anthropic.key"
admin_key_file = ""        # optional Admin API key file, to cross-check spend

[agents.antigravity]
enabled = true
model = "gemini-4-argon-medium"   # the effort is part of the model id
threshold_pct = 90
poll_s = 30

[agents.codex]
enabled = true
model = "gpt-6.1-sol"
effort = "medium"
threshold_pct = 90
poll_s = 15
```

### The Claude API key

Create the key in the Console, in a dedicated workspace with a monthly spend limit, and store it
where only you can read it:

```
mkdir -p ~/.config/baton
( umask 077; cat > ~/.config/baton/anthropic.key )   # paste the key, then Ctrl-D
chmod 600 ~/.config/baton/anthropic.key
```

baton refuses a key file that is not a regular file with mode 600. It reads the key when it
starts the process and passes it only in that process's environment; it is never logged, stored in
state or given to another agent.

### Goal files

A goal is a TOML file. Only `name`, `workdir` and `objective` are required; everything else has the
default shown in [`examples/goal.toml`](examples/goal.toml):

```toml
name = "example"
workdir = "/path/to/project"
objective = "Make the build pass on all platforms."

[verify]
command = "./check.sh"
timeout_s = 3600
```

The verification command runs through the shell in the working directory. Exit 0 finishes the
goal; anything else, including a timeout, means not done, and its output goes to the next round.
Without `verify.command`, the goal is finished when a round completes.

Between rounds each agent gets the same context: the repository, its commits, `.baton/HANDOFF.md`
(kept current by the agents), the last gate output and the tail of the previous round. `.baton/`
lives in the working directory and is added to `.git/info/exclude`. After each round baton commits
uncommitted work locally as `baton: snapshot round <n> (<agent>)` so a hard stop loses nothing. It
never pushes (`snapshot = false` turns the commits off).

### `baton.toml`

When you use baton from OpenCode (below), a `baton.toml` in the project directory plays the role
of the goal file: its `[verify]`, timings and `[agents.*]` settings apply. `name`, `workdir` and
`objective` in it are ignored, since the session decides them. Without one there is no gate, and
the job finishes when a round completes. An invalid `baton.toml` is an error, never a silent
fall-back to no gate.

## Using it from OpenCode

OpenCode sees one OpenAI-compatible provider with one model, `baton/auto`, and keeps the history
and context on its side.

1. Print the provider block and merge it into your `opencode.json`:

   ```
   baton opencode-config
   ```

2. Start the gateway. It binds `127.0.0.1` only:

   ```
   baton serve            # --port 18777 by default
   ```

3. Select `baton/auto` in OpenCode and write your request.

Each OpenCode session becomes a job that runs in its own thread inside `baton serve`, independent of
the HTTP connection. A dropped connection never stops it, and a new message in the session becomes
a note that interrupts the running round. The stream shows the agents' text, one line per rotation,
waits and gate results. Title requests are answered locally and spend nothing. The working
directory is read from OpenCode's own database (`~/.local/share/opencode/opencode.db`, or the path
in `BATON_OPENCODE_DB`); baton never guesses one.

Since jobs live inside `baton serve`, keep it alive. On macOS, use launchd with
[`examples/baton-serve.plist`](examples/baton-serve.plist), after replacing the placeholders:

```
cp examples/baton-serve.plist ~/Library/LaunchAgents/<label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

launchd does not load your shell profile, so the plist sets `PATH` itself; it must reach `baton`,
`codex`, `agy` and `claude`.

## Commands

| Command | What it does |
|---|---|
| `baton doctor [goal]` | Check binaries, logins, stray API keys, live limits and Claude spend. |
| `baton run <goal.toml>` | Run one goal in the foreground, without OpenCode. |
| `baton serve [--port 18777]` | Start the OpenCode gateway. |
| `baton status <goal.toml>` | One-shot status: rounds, cooldowns, recent events. |
| `baton say <goal.toml> [--now] "note"` | Leave a note for the next round. `--now` also interrupts the current one. |
| `baton stop <goal.toml> [--now]` | Stop after the current round, or kill the round with `--now`. |
| `baton opencode-config [--port 18777]` | Print the provider JSON for `opencode.json`. |

State lives in `~/.local/state/baton/<goal name>/` (events, state, raw round logs, inbox), one
runner per goal. `BATON_STATE_DIR` and `BATON_CONFIG` move the state and defaults paths. On macOS
the runner holds `caffeinate` so the machine does not sleep mid-goal.

baton never exits because of a limit, a transient error, a crash in an agent or bad network. Only a
passing gate, a stop request or a termination signal ends it.

## Development

```
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_private.py
```

Tests never call a real vendor and never read real credential files: fake CLIs and fake limit
readers stand in. `scripts/check_private.py` is part of CI. It fails on any e-mail address, home
path, private or Tailscale address, `.ts.net` hostname, UUID or credential-looking token in a
tracked file or a commit message, because this repository must hold no personal data. Working
notes (`.superpowers/`, `.oneshot-state.md`) stay untracked, and a test enforces it.

The design is in [`docs/specs/2026-11-04-baton-design.md`](docs/specs/2026-11-04-baton-design.md).
