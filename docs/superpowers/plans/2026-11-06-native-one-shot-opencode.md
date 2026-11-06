# Native one-shot and OpenCode compaction Implementation Plan

**Goal:** Let one vendor own a complete task until it finishes or reaches its configured limit, preserve cross-vendor conversation context, and answer OpenCode compaction requests in OpenCode's required format.

**Architecture:** Default to one full task per vendor invocation, with Codex's native Goals feature enabled and Baton retaining vendor session IDs. Save a sanitized shared transcript in `.baton/conversations/<goal>.md` so a newly selected vendor can read the full history. Treat title and compaction requests as distinct gateway operations; compaction gets a response-only prompt and no Baton progress events.

**Tech Stack:** Python 3.12 standard library, OpenAI-compatible HTTP/SSE gateway, Codex/Antigravity/Claude CLIs.

**Spec:** User request on 2026-11-06; root cause cross-checked against OpenCode's `packages/core/src/session/compaction.ts` and current Codex Goal documentation.

## Global Constraints

- Keep OpenCode's SQLite access read-only.
- Keep existing per-vendor native session IDs and resume behavior.
- Keep `phases = true` available as an explicit opt-in to Baton-managed plan/build/review.
- Do not expose unmasked credentials in the shared transcript.
- Do not write assistant status chatter into OpenCode compaction output.
- Use only standard-library dependencies.

## Review Focus

1. An OpenCode title request without tools must still return a title; an unrelated no-tools request must start normal work.
2. A compaction request must receive the full conversation and the exact OpenCode summary instruction, with no code-task wrapper or Baton event text.
3. A vendor switch must include all stored vendor/session IDs and the path to the complete sanitized transcript.
4. A direct vendor selection must not silently run a different configured vendor.
5. Existing explicit phase mode and old state files must remain readable.

---

### Task 1: OpenCode title and compaction routing

**Files:** Modify `src/baton/gateway.py`, `src/baton/config.py`, `src/baton/runner.py`.

- Recognize title requests by the title prompt marker, not only by the absence of tools.
- Recognize OpenCode compaction prompts and preserve all user, assistant, and tool message content for the summary input.
- Add an internal response-only mode: no phase instructions, no Superpowers requirement, no session resume, no file snapshot, and only model text reaches the SSE response.

### Task 2: Whole-task vendor turns and shared handoff context

**Files:** Modify `src/baton/config.py`, `src/baton/runner.py`, `src/baton/scheduler.py`, `src/baton/store.py`, `src/baton/adapters/codex.py`; add `src/baton/oneshot.py`.

- Make full-task execution the default; explicit `phases = true` retains Baton phases.
- Enable Codex's native Goals feature and request that the Codex session set and finish its native goal.
- Replace the default “one shift of a relay” prompt with whole-task ownership instructions.
- Append sanitized prompts and outputs to `.baton/conversations/<goal>.md`, including vendor and session ID metadata.
- Include prior vendor names/session IDs and the full transcript path in new or resumed vendor turns.
- Persist an optional exact next-vendor request and have the scheduler wait for that vendor rather than silently selecting another.

### Task 3: Operator help and docs

**Files:** Modify `src/baton/commands.py`, `src/baton/gateway.py`, `src/baton/cli.py`, `README.md`, and examples.

- Add `baton help` and `baton: help` with CLI and chat command examples.
- Add `baton use <job> <vendor>` and `baton: use <vendor>` for Codex, Antigravity, or Claude API.
- Document how to configure the Claude API key, check it with `baton doctor`, and route a session to `claude_api`.
- Update the default one-shot and optional phases documentation.
