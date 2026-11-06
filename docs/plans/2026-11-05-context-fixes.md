# Round context: resume antigravity, keep the person's words, keep the top of the handoff

## Why

Evidence from a live 39-round job, whose fresh prompt was 64 KB:

1. Antigravity never resumes. 19 of 19 rounds were fresh (Codex: 16 of 19 resumed), and `state.json`
   has no `antigravity:*` session. Real first line of a round log:
   `{"event":"init","conversation_id":"38059c91-...","init":{"model":"...","cwd":"..."}}`.
   The id is at the top level, the adapter reads it from the `init` payload, and
   `tests/fakes/fake_agy.py` nests it, so the tests pass against a shape agy does not send.
2. The history is cut by its last 24000 characters, so one long assistant stream pushes out what
   the person said: only 6 of 18 messages survived and the goal was gone. `history_text`
   (`src/baton/gateway.py`) cuts it, and so does the function that builds the saved context around
   line 296, which stops at the same size and loses older user messages for good.
3. `read_handoff` keeps the last 16000 characters of a 57 KB file, which drops its top: the active
   objective and the person's authorization.

## Global constraints

- Conversations under the limits give exactly today's text, and `text_of`-level parsing stays.
- `read_handoff(limit=False)` stays whole.
- Do not change the resume prompt, the session key logic or `build_model` defaults.

## Task 1: Three context fixes

**Files:** `src/baton/adapters/antigravity.py`, `src/baton/gateway.py`, `src/baton/runner.py`,
`tests/fakes/fake_agy.py`, `README.md`, and their tests.

### Antigravity session id

- In `run_round`, on the `init` event, take the id from `event.get("conversation_id")`, falling
  back to the payload's, and keep the current non-empty-string checks. Only `init` sets it, as today.
- `tests/fakes/fake_agy.py` must emit the real shape: `conversation_id` at the top level of `init`
  and not inside it. Add a way to emit the old nested shape only if a test needs the fallback.
- Check that a second round really passes `--conversation <id>`, through the runner with the fake.

### History

`history_text`, when the joined text exceeds `HISTORY_MAX_CHARS`:

- The person's messages are kept whole, newest first, while they fit in the limit. If they do not
  all fit, the oldest go.
- What is left of the limit goes to assistant messages, newest first; the last one that does not
  fit keeps its tail. The rest are left out.
- The result stays in conversation order, and each place where something was left out holds one
  `...` line, however many messages were dropped there.
- The total never exceeds `HISTORY_MAX_CHARS` plus those markers and the role prefixes.

The saved-context function near line 296: stop losing user messages. Keep every user message and
apply the size stop to assistant messages only; `job.json` is rewritten with each message, so
keep its cap on assistant text at the same limit.

### Handoff

`read_handoff(limit=True)`, for a file over `HANDOFF_MAX_CHARS`: the first `HANDOFF_HEAD_CHARS`
(new constant, 4000) plus a marker line stating how many characters were left out and that the
file holds them, plus the last `HANDOFF_MAX_CHARS - HANDOFF_HEAD_CHARS`. A file within the limit is
returned unchanged.

### README

Say in the section on what a round receives how the history and the handoff are trimmed, in
one or two sentences in the README's style.

### Tests (write first; each must fail before the change)

- Adapter and fake: the session is saved from the real `init` shape; a nested id still works; the
  next round's argv has `--conversation`.
- History: under the limit, unchanged output; a huge assistant message between short person
  messages keeps every person message in order; person messages over the limit lose the oldest;
  the tail of the newest assistant message is kept; a marker appears at each gap, once.
- Saved context: a huge assistant message does not lose earlier user messages.
- Handoff: under, exactly at, and over the limit; head and tail both present; the marker's count
  is correct; `limit=False` stays whole.

### Verify

`uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`
