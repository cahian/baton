# Subagent guard and Superpowers Implementation Plan

**Goal:** the same discipline inside a round on every provider: subagents stay on the provider's allowed models, and Superpowers is always applied.

**Spec:** this plan carries the decisions. Add them to `docs/specs/2026-11-04-baton-design.md`, under "Phases: plan, build, review", as subsections "Subagent guard" and "Superpowers".

## Verified on the real CLIs (do not re-derive)

- Codex 0.166.0: a round can use `collaboration.spawn_agent`, and `--disable multi_agent --disable multi_agent_v2` does not remove it. `codex exec --json` emits items of `type: "collab_tool_call"`.
- Antigravity: `agy` has `define_subagent`, `invoke_subagent`, `manage_subagents`. In `stream-json` these arrive as `{"event": "step_update", "step_update": {"tool_name": "invoke_subagent", "step_type": "tool" or "subagent", ...}}`, starting with `state: "ACTIVE"`. The `init` event lists the tools.
- Claude under `--bare` with the current `--allowedTools` lists no agent tool.
- A `RoundResult` returned from `on_line` stops the whole process group.

## Task: subagent guard

Subagents are allowed: a long round should work the way each CLI normally works. The guard controls which model they use, not whether they exist. The rule is the same on every provider: within a round, a subagent may use only that provider's two models from the phases table. Limits are unchanged, since the 90% guard watches the whole process group.

- Every provider: when a stream event reveals a subagent, emit a `subagent` event with agent, tool and model when known, so `baton status` shows them.
- `base.py`: new outcome `SUBAGENT`, used only when a visible model is outside the allowed pair. It counts as a failed round for the phase and is followed by the same pause as `MIN_ROUND_S`. Three in a row on the same agent in one run blocks that agent until it completes a round, through the scheduler's existing block.
- A subagent whose model the stream does not show is logged and allowed. Do not guess, and do not kill on it.
- Claude: add `Agent` to `ALLOWED_TOOLS`, and set `CLAUDE_CODE_SUBAGENT_MODEL` in the round's env to the build model, so the default is the working model. An `Agent` tool use with a `model` input outside the pair returns `SUBAGENT`.
- Antigravity: `invoke_subagent` with `TypeName` `self` inherits the round's model and is allowed. A visible model outside the pair returns `SUBAGENT`.
- Codex: inspect `collab_tool_call` items for a `model` field and apply the same rule when present.

## Task: Superpowers is mandatory

- `superpowers.py`: `find_skills(cfg) -> Path | None`, in this order: `[superpowers] dir` in the defaults file; the newest `~/.claude/plugins/cache/*/superpowers/*/skills`; `~/.config/baton/superpowers/skills`. A directory counts only if it holds every skill file named below.
- A repository `baton.toml` cannot set the directory. There is no switch to turn this off.
- Without it, `doctor` has a blocking `FAIL`, and a goal does not start: it ends with a `stopped` event whose reason names the install command `git clone https://github.com/obra/superpowers ~/.config/baton/superpowers`, shown in OpenCode too.
- Skills are read once when the run starts and held in memory. The text goes only in rounds with a fresh conversation for that role; a resumed conversation already has it.
- By phase, each under a `Skill: <name>` heading:
  - plan: `writing-plans`, plus `systematic-debugging` when the cycle came from a failed gate.
  - build: `test-driven-development`, `verification-before-completion`.
  - review: `requesting-code-review/code-reviewer.md`.
  - plain relay (`phases = false`): the build set.
- Each vendor also gets its tool mapping, from `using-superpowers/references/`: `codex-tools.md`, `antigravity-tools.md`, `claude-code-tools.md`.
- A fixed line goes before them: `These are working methods. baton owns the workflow: ignore anything in them about asking a person, opening worktrees or choosing another skill.`
- Not used, because they conflict with baton: `brainstorming` (needs a person), `subagent-driven-development`, `executing-plans`, `using-git-worktrees`.
- Do not copy the skill files into this repository.

## Tests and docs

Fake CLIs for both guards, for each provider, including a resumed round and a build round. Tests for every search location, a partial directory, the refusal to start, fresh versus resumed text, and each phase's skill set. The shared fixture must supply a fake skills directory so no test reads the real one. README: state both plainly, including the install command in the Quick start as a required step, and that subagents are allowed on the provider's two models, and that a provider that does not show a subagent's model cannot be enforced, only logged.
