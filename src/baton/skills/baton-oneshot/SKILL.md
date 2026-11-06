---
name: baton-oneshot
description: Use when Baton starts or resumes a coding task. Own the complete objective in this vendor session, use native skills and long-running task features, verify every deliverable, and let Baton hand off only at a configured limit or when the user requests a switch.
---

# Baton one-shot task

Use this skill whenever Baton asks you to work on a goal.

## Start with the complete context

1. Read the full sanitized conversation file named in the Baton prompt. It contains the user's
   original request, prior vendor work, and the native session IDs Baton has saved.
2. Read the repository's `AGENTS.md`, equivalent vendor instructions, and the relevant project
   documentation before changing files.
3. Inspect the working tree. Preserve the user's existing changes and continue from the actual
   repository state; do not assume an earlier vendor's summary is complete.
4. Treat the user's requested outcome and completion conditions as the scope. Ask a question only
   when a real blocker prevents a safe, useful next step.

## Own the whole task in this vendor

- Continue through implementation, relevant verification, and a final review. Do not return after
  only making a plan, doing one partial change, or suggesting that another vendor should finish.
- Use this vendor's native tools and invoke relevant native skills. Use native subagents when they
  materially help, while keeping responsibility for the complete result in this session.
- Keep work in this vendor's session until it is complete, the user explicitly asks to switch, or
  Baton reaches a configured usage or turn limit. Do not request an early handoff for convenience.
- Do not invoke Baton recursively or ask it to call the same vendor again to do work you can finish
  in this session.
- At a genuine limit, leave accurate progress in the repository and conversation. Baton carries
  the complete sanitized transcript and all saved vendor session IDs to the next vendor. Never
  report the task as complete when deliverables remain.

## Use the vendor's native long-running workflow

- **Codex:** create or continue the native Goal for the requested outcome, keep it active while
  work remains, and complete it only after checking every deliverable.
- **Antigravity:** use the native `/goal` mode for the objective. Baton starts it for a new
  Antigravity session; on a resumed session, continue the existing goal.
- **Claude Code:** use this native skill and Claude Code's available plan, task, and session tools.
  This Baton integration does not claim a persistent Goal primitive for Claude Code.

## Finish with evidence

1. Check each requested deliverable against the user's request and the full conversation.
2. Run the relevant project checks when appropriate; report checks you did and their results
   accurately. Do not imply that a check passed if it was not run.
3. Review the final diff for accidental changes and preserve unrelated work.
4. Return a concise summary of what changed, verification performed, and any remaining blocker.
   Use the user's language when practical.
