# Antigravity output: join text fragments, show tool steps

## Why

Raw round logs show two defects in how baton shows an `agy` round.

1. `step_update.text_delta` is a fragment that can end mid-word
   (`'... (file'`, `':///path/to/L'`, `'ichii/.baton/PLAN.md'`), but the
   runner and the gateway treat it as a whole line and add `\n` after each one. The same broken
   text also lands in `round_tail`, which later rounds receive.
2. Tool steps are dropped by `render()`, so a round of hundreds of tool steps shows nothing until
   the final answer. Rounds 35 and 36 had 171 and 269 events and no text at all.

Observed event shape:

    {"event":"step_update","step_update":{"step_index":9,"state":"ACTIVE","step_type":"tool",
     "tool_name":"run_command","tool_info":{"name":"run_command",
     "parameters":{"CommandLine":"git status"}}}}

Each tool step is reported once as `ACTIVE` and once as `DONE`. Observed tools and first
parameter: `run_command` (`CommandLine`), `read_file` (`Path`), `edit_file` (`Path`), `search_web`
(`query`), `manage_task` (`Action`). Text only comes on `step_type: agent_response`. Across every
stored log, consecutive agent responses with no tool between them always ended in `\n`, so no
separator between steps is needed.

## Global constraints

- Claude and Codex output must not change: an adapter without `is_fragment` behaves as today, and
  an `output` event without `fragment` renders as `text\n`.
- Only the antigravity adapter emits fragments.
- `render()` stays stateless: `is_work` calls it again on the same line.

## Task 1: Fragments and tool lines

**Files:** `src/baton/adapters/antigravity.py`, `src/baton/runner.py`, `src/baton/gateway.py`,
`tests/test_antigravity.py`, `tests/test_gateway.py`, and the runner's test file.

### `src/baton/adapters/antigravity.py`

- `render()`, in the `step_update` branch: a non-empty string `text_delta` is returned unchanged,
  whitespace included (never stripped). Otherwise return the tool line described next, or `None`.
- Tool line, a module-level helper: only for `step_type == "tool"`, `state == "ACTIVE"` and a
  non-empty string `tool_name`, so that a step shows once, as it starts. `parameters` is
  `tool_info.parameters`; tolerate either being missing or not a dict.
  - `run_command` with a non-blank string `CommandLine`: `$ <brief>`.
  - any other tool: `<tool_name>: <brief>` of the first non-blank string value in `parameters`,
    in order; `<tool_name>` alone when there is none. `run_command` without a usable
    `CommandLine` falls into this rule.
- `<brief>`: the first line of the stripped value, cut to 200 characters (name the constant), with
  ` …` appended if anything was left out (further lines or characters).
- New method `is_fragment(self, line: str) -> bool`: true only for a `step_update` with a non-empty
  string `text_delta`. Tool lines, `error:` lines and non-JSON lines are whole lines.
- `is_work` keeps its code, so tool steps now count as work. That is intended: an agent running
  tools has read its prompt.

### `src/baton/runner.py`, in the round function around `on_output`

- Ask the adapter through `getattr(adapter, "is_fragment", None)`, the way `is_work` is asked, and
  inside the same `try`, so an exception gives no output.
- Track `mid_line`, true while the text shown so far stops short of a line end.
- A fragment: append `text` to `buf` with no `\n`; set `mid_line = not text.endswith("\n")`; emit
  `output` with `text=text, fragment=True`.
- A whole line: first close the line (below), then behave exactly as today (no `fragment` field).
- Close the line, a small local function: when `mid_line`, append `\n` to `buf`, emit `output` with
  `text="\n", fragment=True` and clear `mid_line`.
- Close the line when an attempt ends, however it ends, before any other event of that attempt:
  after `adapter.run_round` returns and at the start of its `except Exception` branch. This must
  also cover the `resume_failed` retry, where `mid_line` must be false again with `buf` reset.
- `buf` keeps its `[-ROUND_TAIL_CHARS:]` limit in every path.

### `src/baton/gateway.py`

`render_event`, `kind == "output"`: return the text unchanged when `f.get("fragment")`, else
`f"{text}\n"` as today.

### Tests (write them first; each must fail before the change)

- `render`: `run_command` ACTIVE gives `$ git status`; the same step DONE gives `None`;
  `read_file` gives `read_file: /a/b`; no parameters gives the bare tool name; missing or
  non-dict `tool_info` does not raise; a multi-line command gives its first line plus ` …`; a
  201-character value is cut to 200 plus ` …`, and one of exactly 200 is not; a `text_delta` of
  `"\n\n"` comes back unchanged.
- `is_fragment`: true for a delta; false for a tool line, `error:`, plain text, and invalid JSON.
- `test_only_the_agent_acting_counts_as_work`: add a tool step (`work=True`) and a DONE tool
  step, which renders nothing, so that test's `assert adapter.render(line)` needs it kept apart.
- Gateway: `render_event("output", {"text": "rende", "fragment": True}) == "rende"`, and without
  `fragment` the text gets its `\n`.
- Runner, with a fake adapter whose lines are fragments and whole lines: the rendered text is
  exactly what a person should read, for `"rende"`, `"rização\n"` → `"renderização\n"`; for `"abc"`
  then a whole line `$ ls` → `"abc\n$ ls\n"`; for a round ending on `"abc"` → `"abc\n"` before
  `round_end`, also when the adapter raises; `round_tail` matches. An adapter without
  `is_fragment` gets the same events as before.
- End to end: the real `AntigravityAdapter` with `tests/fakes/fake_agy.py`, sending split deltas
  and a tool step through `FAKE_AGY_EXTRA`, joined through `render_event`, gives unbroken text.

### Verify

`uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`
