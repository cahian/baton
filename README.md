# baton

![baton — One goal. Keep it moving. Three connected agents pass a glowing relay baton.](docs/assets/baton-banner.png)

baton keeps one long coding goal moving for days by passing it between coding agents, stopping
each one before it can spend money it should not. It works with Codex and Antigravity alone;
Claude is an optional third, a paid backup that only joins once you save an API key for it.

You give it a goal (an objective, a working directory and a verification command). It runs one
agent until that agent nears its limit, hands the work to the next, and when every agent is out,
sleeps until the earliest reset and carries on. It finishes only when the verification command
exits 0; an agent saying "done" means nothing.

Every agent runs through its vendor's own CLI. baton reimplements no agent. Context moves between
them through the repository, its commits and a handoff file, and each vendor's own conversation
is resumed when its turn comes again.

## Quick start: from zero to your first task

This walks you through everything once, with no prior knowledge of baton. It takes about 15
minutes. The commands assume macOS; on Linux everything works except the background service, so
use the note in step 4.

**Before you start.** baton does not bring its own AI. It drives the coding tools you already
pay for, so you need accounts and their command-line tools:

| Tool | What it is for | Required? |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | installs baton | yes |
| [OpenCode](https://opencode.ai) | the chat window you type in | yes, for this guide |
| `codex` | OpenAI Codex, signed in to your plan | at least one of these two |
| `agy` | Google Antigravity, signed in to your plan | at least one of these two |
| `claude` | Claude Code, an optional paid backup; baton works without it | no, optional |
| [ai-memory](https://github.com/akitaonrails/ai-memory) | long-term memory across goals, see [below](#long-term-memory) | no |

### 1. Install baton

```
git clone https://github.com/cahian/baton.git
cd baton
uv tool install .
```

### 2. Create your settings

```
mkdir -p ~/.config/baton
cp examples/defaults.toml ~/.config/baton/defaults.toml
```

Claude is optional. Open `~/.config/baton/defaults.toml` in any text editor and pick one of these:

- **No Claude (the simplest):** change nothing, and skip step 3. Without a saved key, Claude never
  runs and baton uses Codex and Antigravity only. To switch it off for good, set `enabled = false`
  under `[agents.claude_api]`.
- **With the Claude backup:** set `monthly_budget_usd` to the credit you actually have, and
  `budget_reset_day` to the day of the month it renews. baton stops 10% before that number. Then
  do step 3.

Change `enabled` for `codex` or `antigravity` the same way if you only have one of them.

### 3. Add the Claude key (optional)

Create an API key in the [Claude Console](https://platform.claude.com) and copy it. Then run this,
which saves what you copied into a file only you can read:

```
touch ~/.config/baton/anthropic.key
chmod 600 ~/.config/baton/anthropic.key
pbpaste > ~/.config/baton/anthropic.key
```

Do not paste the key into a chat or into a project.

**Check your billing first.** baton cannot see or change it. If the Console has auto-reload on, or
holds credits you bought, using the key can spend that money. Turn auto-reload off.

### 4. Start baton and check it

```
baton install-service
baton doctor
```

`install-service` keeps baton running in the background, even after a restart. Every line of
`doctor` should read `ok`; with no Claude key it says `claude_api: not configured (optional)`,
which is fine. `FAIL` tells you what to fix; `warn` is only a heads-up. It spends
nothing, so run it as often as you like.

> **On Linux**, skip `install-service` and leave `baton serve` running in a terminal.

**Optional: long-term memory.** If you want agents to remember what earlier goals learned in the
same directory, put the `ai-memory` binary on your `PATH`; baton does the rest. `doctor` shows a
`memory` line either way. Without the binary baton works exactly as before. The details, including
how to install it, are in [Long-term memory](#long-term-memory).

### 5. Add baton to OpenCode

```
baton opencode-config
```

This prints a block of settings. Where it goes depends on whether you already use OpenCode:

- **No `~/.config/opencode/opencode.json` yet:** save it directly:

  ```
  mkdir -p ~/.config/opencode
  baton opencode-config > ~/.config/opencode/opencode.json
  ```

- **You already have that file:** do not run the command above, it would erase your settings.
  Open the file and copy the `baton` entry into your existing `provider` section.

Then tell OpenCode to read the change. If you skip this, baton will not show up:

```
opencode reload
```

### 6. Connect it, once

Use a throwaway folder for your first try. **The agents have full access to the folder where you
open OpenCode**, so do not start in a project you care about:

```
mkdir -p ~/baton-test
cd ~/baton-test
git init
opencode
```

1. Type `/models`. Searching for `baton` shows **No results found**. That is expected the first
   time.
2. Press `ctrl+a` to see all integrations, and choose **baton**.

   ![The integration list with baton selected](docs/assets/tutorial-connect.png)

3. It asks for an API key. baton does not need one, but the box cannot be empty. Type `local`
   and press Enter.

   ![The API key prompt](docs/assets/tutorial-api-key.png)

4. Type `/models` again and choose **Auto**. If OpenCode asks for a variant, keep **Default**.
   baton ignores this setting; effort is set in your `defaults.toml`.

   ![The variant list with Default marked](docs/assets/tutorial-variant.png)

You are ready when the box at the bottom says **Auto baton**:

![OpenCode with the Auto model from baton selected](docs/assets/tutorial-ready.png)

OpenCode remembers the connection, but check that label whenever you open a new session.

### 7. Send your first task

```
Create a hello.py that prints "hi" and run it to check.
```

The answer streams in the chat like any other model. A line tells you each time one agent hands
over to another. While it works:

- **To change direction,** just send another message. It interrupts the current round.
- **Closing OpenCode does not stop the work.** It keeps running in the background.

### 8. Watch it, or stop it

Run these in another terminal:

```
baton jobs                   # everything running; names look like session-<id>
baton status session-<id>    # where it is and what it has spent
baton stop session-<id>      # stop it
baton doctor                 # limits and spending right now
```

### 9. Let it run until the job is really done

By default, the work ends after one round. For a long goal, give baton a test to pass: create a
file named `baton.toml` in your project with the command that proves the job is finished.

```toml
[verify]
command = "./check.sh"
timeout_s = 3600
```

baton then keeps going, across agents and limit resets, until that command succeeds. An agent
saying "done" does not count. Without a good check it may stop too early, and with an impossible
one it will keep trying, so choose something you trust.

### If something goes wrong

| What you see | What to do |
| --- | --- |
| `baton` is not in `/models` | Run `opencode reload`, reopen OpenCode, then do step 6. |
| It asks for an API key | Type `local`. |
| The model still is not listed | In your project, run `echo '{"model":"baton/auto"}' > opencode.json`, then reopen OpenCode. |
| The answer never starts | Run `baton doctor` and `baton jobs`. |
| It says it is waiting | Every agent hit its limit. It resumes alone at the next reset. |
| You want to go back to another model | Choose it in `/models`. Nothing else changes. |

## The rotation

The default order, all at medium effort:

1. **GPT-6.1 Sol**, through Codex.
2. **Gemini 4 Argon**, through Antigravity (`agy`).
3. **Claude Opus 5.5**, through a Console API key (never a subscription login).

Codex and Antigravity are the plan agents: their included usage comes back at a reset. Claude is
paid by use and has no reset that would ever stop it, so it is a fallback, wherever it stands in
`order`. **Claude runs only while both others are out.**

**Claude is optional.** baton works with Codex and Antigravity alone, and that is the default
experience: until you save a key file (Quick start, step 3), Claude never runs and the rotation is
the other two. It joins as a backup only once the key is there. To turn it off for good, set
`enabled = false` under `[agents.claude_api]` in `defaults.toml`, or leave it out of `order` in a
goal file. `baton doctor` reports an unconfigured Claude as one `ok` line, not a problem.

The first plan agent in `order` starts. The current one keeps working until it is unavailable,
then the next plan agent takes over, wrapping around. A plan agent that comes back does not
preempt another plan agent that is working. When both are out, Claude runs. While it does, baton
reads the plan agents (without a model call) from their reset time onward, at most every 30
seconds, and as soon as one is available it ends the Claude round and that agent takes over. If
both are, the first in `order` goes first. This is not a failure: Claude gets no cooldown. A plan
agent whose round fails within five minutes of such a takeover stops being allowed to end Claude
rounds until it completes one. At most six Claude rounds a day are ended this way (`baton
status` says when the cap is active). When no agent can run, baton waits for the earliest known reset.
Change the order, or disable an agent, in the goal file.

## Long-term memory

`.baton/HANDOFF.md` is short-term: it dies with the goal. [ai-memory](https://github.com/akitaonrails/ai-memory)
is the long-term layer: what past rounds did and learned, searchable, across goals in the same
directory. It adds to the handoff and never replaces it. It is optional: without the binary, or
with memory off, nothing changes, and a memory failure can never stop a goal.

**baton drives it; the agents do not.** baton calls the `ai-memory` command itself, between rounds,
so the same memory reaches Codex, Antigravity and Claude alike. It uses no hooks and no MCP, and
never runs `ai-memory setup`, `install-hooks`, `install-mcp` or `run`.

**What is stored, and where.**

- Before every round, baton searches the project's memory with words from the last verify output
  (at most 16 of them) and the objective (the rest, up to 24 in all), and puts the best 5 hits (6000 characters at most) in the prompt, labelled as
  context that may be out of date, not as instructions. Words are taken from the end of the
  verify output and of the objective, where the errors and the newest message are. The hits sit
  between `--- BEGIN BATON MEMORY ---` and `--- END BATON MEMORY ---`, one line each, starting
  with the page's date and goal.
- After a round that started a process, baton stores one page: the date, the agent, how it ended,
  how long it took, the gate result, the commits it made, and the tails of the verify output, the
  handoff and the round. It is written when the round ends and again, over itself, once the
  verify has run, so a baton killed during the verify still leaves its page. A round that fails,
  times out or has a failing verify is kept as a "gotcha"; a stop, an interrupt, a takeover by
  another agent or a limit is normal rotation.
- Agents are told to keep durable findings under a `## Lessons` heading in the handoff. When that
  section changes, baton stores it as a pinned page that outlives the goal.
- ai-memory redacts secrets before it stores anything. Memory is per directory: a project is the
  directory's name plus a short hash of its full path, and a search never leaves it.
- The data is in `~/.local/state/baton/.ai-memory` (mode 700), with its own config and its own
  port, `127.0.0.1:18778`, so it never meets another ai-memory on the machine. The leading dot
  keeps it clear of every goal's state directory, since no goal can be named that.
  `BATON_STATE_DIR` moves it.
  baton starts the server when it first needs one and leaves it running.
- ai-memory gets only `PATH`, `HOME`, `TMPDIR`, `NO_COLOR` and `RUST_LOG` in its environment, so no
  provider key can reach it, and baton configures no model for it: it uses none.

**Install it.** Download the release for your platform from the
[ai-memory releases](https://github.com/akitaonrails/ai-memory/releases), and put the `ai-memory`
binary on your `PATH` (or set `bin` below to its path). Do not run `ai-memory setup`: it wires
hooks into every session on the machine, which is what baton avoids. Then run `baton doctor`: the
`memory` line shows the binary found and whether the server is up. If you run baton as a service,
re-run `baton install-service` after installing ai-memory, so the service can find it.

**Turn it off.** `memory = false` in a goal file or `baton.toml` turns it off for that project.
`BATON_MEMORY=off` in the environment turns it off everywhere. In `defaults.toml`:

```toml
[memory]
enabled = true
bin = "ai-memory"      # a name on PATH, or a path
port = 18778           # 1024-65535
recall_chars = 6000    # 0-20000; 0 stops recall, recording goes on
```

A `baton.toml` that comes with a repository can only turn memory off or on; it cannot set `bin`
or `port`. A `[memory]` table that cannot be read turns memory off, with one line in `baton status`
and a warning in `baton doctor`.

**Limits.**

- One memory call, starting the server included, is given 8 seconds in all. After any failure
  (server dead, garbage output) baton skips memory for 300 seconds, so a broken memory costs a
  round one short wait at most. A missing binary costs nothing and says nothing; `baton doctor`
  warns. Failures show in `baton status` as `memory` events, never in the chat.
- If you change `port` while the old server is still running, baton starts no second one: it says
  so, in `baton status` and `baton doctor`, and you stop the old server (or set the old port
  back). baton never stops it for you.
- A round that baton had to kill is still recorded, as `interrupted`.
- Recall is keyword search, not understanding: it finds earlier pages that share words with the
  failing output or the objective. It can miss, and it can return something stale.
- Lessons longer than 10000 characters are cut to their last 10000.
- Goals in one directory share one memory; the pages are told apart by goal name.

## How each agent is stopped before it costs money

The failure this exists to prevent: at 100% of its included usage, the Codex CLI keeps working
and silently debits account credits. No error is raised. A design that waits for a limit error
has already paid, so baton reads the limits and acts before they are reached.

| Agent | What is read | Stop condition |
|---|---|---|
| Codex | `codex app-server` rate limits and credit balance. No model call, nothing spent. | 90% used in any window, or the credit balance drops |
| Antigravity | the `/usage` and `/credits` slash commands. No model call, nothing spent. | 90% used in any Gemini row, or the credit balance drops. Both `Gemini Models` rows, Five Hour and Weekly, must be readable |
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
- Before a round, baton reserves `round_budget_usd` (default 5), passed to Claude as
  `--max-budget-usd`. If the ceiling would be passed, the round shrinks to what is left, and below
  5 USD the agent is unavailable until the next period.
- A round that ends without a result keeps its whole reservation counted. Crashes overcount and
  never undercount. So does a round ended because a plan agent came back: it has no result, and
  whatever it spent before is counted as the whole 5 USD. The ledger drifts high, never low, and
  the small default keeps that drift small. A corrupt ledger makes the agent unavailable; it is never recreated silently.
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
  depend on it: `baton jobs` and `baton status session-<id>` show where it is, and a new message
  in the session reattaches the stream.

## Install

```
uv tool install .
baton doctor
```

Python 3.12 or newer. There are no runtime dependencies. You also need the CLIs you want to use on
your `PATH` and logged in: `codex`, `agy` and `claude`. Binary paths can be overridden per agent
with `bin`, in a goal file or in `defaults.toml`, since tmux and launchd may not load your shell
aliases. A leading `~` is expanded.

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
round_budget_usd = 5       # most one round may spend (a killed round counts all of it)
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
lives in the working directory and is added to `.git/info/exclude`. With `ai-memory` installed, what
earlier goals learned in the directory is added too (see [Long-term memory](#long-term-memory);
`memory = false` turns it off). After each round baton commits
uncommitted work locally as `baton: snapshot round <n> (<agent>)` so a hard stop loses nothing. It
never pushes (`snapshot = false` turns the commits off).

### `baton.toml`

When you use baton from OpenCode (below), a `baton.toml` in the project directory plays the role
of the goal file: its `[verify]`, timings and `[agents.*]` settings apply. `name`, `workdir` and
`objective` in it are ignored, since the session decides them. Without one there is no gate, and
the job finishes when a round completes. An invalid `baton.toml` is an error, never a silent
fall-back to no gate.

A `baton.toml` comes with the repository, so it may not set `bin`, `key_file`, `admin_key_file`,
`full_access` or `poll_s`: a file in a repository must not choose executables or credentials, or
slow the guard's limit reads. A goal file you pass yourself and the defaults file may set `poll_s`.
Everywhere, `threshold_pct` may not go above 90, and an `antigravity.model` must start with
`gemini-`.

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
a note that interrupts the running round. A message gets at least one round, even if the gate
already passes. A request that repeats what was already accepted, as a client's retry does, attaches
to the stream and interrupts nothing, so it costs no round. `baton jobs` lists every job, and
`baton status`, `baton say` and `baton stop` accept its name (`session-<id>`) in place of a goal
file. The stream shows the agents' text, one line per rotation, a sentence when a returning plan
agent ends a Claude round, waits and gate results, and when no agent can run it says why, for
each one. Title requests are answered locally and spend nothing. The
working directory is read from OpenCode's own database (`~/.local/share/opencode/opencode.db`, or
the path in `BATON_OPENCODE_DB`); baton never guesses one.

One runner works in a directory at a time, across `baton run` and `baton serve`. A directory inside
or around a running one counts as busy, since the two would commit each other's work.

### Keeping it running

Jobs live inside `baton serve`, so keep it alive. If it dies, even by `kill -9`, the next
`baton serve` first ends every agent left behind that it can prove is baton's own, then resumes
every job that had not finished. A job a person stopped stays stopped. A group that cannot be
verified is left alive and its job waits, because two agents at once could spend twice.

On macOS, install it as a launchd service:

```
baton install-service            # --port 18777 by default
```

launchd loads no shell profile, so this resolves everything from the shell you run it in. It finds
each enabled agent's binary and `node`, which `codex` needs, builds `PATH` from their directories,
writes `~/Library/LaunchAgents/local.baton.serve.plist` with absolute paths, and loads it. Running
it again replaces the service. The log is `serve.log` in the state directory. Run it again when an
agent or `node` moves. Your verification commands get that same `PATH`, so a tool they need from
elsewhere must be called by its full path. A verification command that exits 126 or 127 can never
pass, so baton says so once in the chat and in `baton status`, and the rounds go on.

[`examples/baton-serve.plist`](examples/baton-serve.plist) shows the same file with placeholders,
for writing it by hand.

## Commands

| Command | What it does |
|---|---|
| `baton doctor [goal]` | Check binaries, logins, stray API keys, live limits and Claude spend. |
| `baton run <goal.toml>` | Run one goal in the foreground, without OpenCode. |
| `baton serve [--port 18777]` | Start the OpenCode gateway. |
| `baton jobs` | List every job and goal that has state: whether its runner is alive, round, current agent, last event. |
| `baton status <goal.toml or job>` | One-shot status: rounds, cooldowns, recent events. |
| `baton say <goal.toml or job> [--now] "note"` | Leave a note for the next round. `--now` also interrupts the current one. |
| `baton stop <goal.toml or job> [--now]` | Stop after the current round, or kill the round with `--now`. |
| `baton opencode-config [--port 18777]` | Print the provider JSON for `opencode.json`. |
| `baton install-service [--port 18777]` | Write the launchd plist and load it (macOS). |

State lives in `~/.local/state/baton/<goal name>/` (events, state, raw round logs, inbox), one
runner per goal. `BATON_STATE_DIR` and `BATON_CONFIG` move the goals' state and the defaults file.
`BATON_STATE_DIR` does not move the Claude ledger: it stays at
`~/.local/state/baton/claude-spend.json` under the real home directory, so that no goal can end up
with a ledger, and a budget, of its own. On macOS the runner holds `caffeinate` so the machine does not sleep mid-goal.

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
