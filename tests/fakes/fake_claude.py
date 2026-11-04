"""Stand-in for the `claude` CLI in print mode. Never talks to a vendor.

Every call is appended to `FAKE_CLAUDE_CALLS` (argv, cwd, stdin and the Anthropic-related
environment, one JSON object per line). The init event reports `apiKeySource` the way the real CLI
does: "ANTHROPIC_API_KEY" when that variable is set, "none" otherwise, unless overridden. Knobs:
  FAKE_CLAUDE_SESSION_ID      id announced in the init event (the --resume id wins when present)
  FAKE_CLAUDE_API_KEY_SOURCE  overrides apiKeySource
  FAKE_CLAUDE_NO_INIT         no init event
  FAKE_CLAUDE_ASSISTANT_FIRST an assistant event before the init event
  FAKE_CLAUDE_TEXT            text of the assistant message (default "working on it")
  FAKE_CLAUDE_ECHO_KEY        also prints the API key inside the assistant text
  FAKE_CLAUDE_SLEEP           seconds to stay alive before the result event
  FAKE_CLAUDE_COST            total_cost_usd, written into the JSON verbatim (default 0.0123)
  FAKE_CLAUDE_NO_RESULT       no result event
  FAKE_CLAUDE_IS_ERROR        the result is an error; its text is FAKE_CLAUDE_ERROR_TEXT
  FAKE_CLAUDE_SUBTYPE         result subtype (default success)
  FAKE_CLAUDE_RESUME_FAIL     with --resume: complain on stderr and exit 1 before any event
  FAKE_CLAUDE_EXIT            exit code (default 0)
  FAKE_CLAUDE_PIDFILE         gets the pid
"""

import json
import os
import sys
import time
from pathlib import Path


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    env = os.environ
    argv = sys.argv[1:]
    if env.get("FAKE_CLAUDE_PIDFILE"):
        Path(env["FAKE_CLAUDE_PIDFILE"]).write_text(str(os.getpid()))
    prompt = sys.stdin.read()
    if env.get("FAKE_CLAUDE_CALLS"):
        record = {
            "argv": argv,
            "cwd": os.getcwd(),
            "stdin": prompt,
            "env": {k: v for k, v in env.items() if k.startswith(("ANTHROPIC", "CLAUDE_CODE"))},
        }
        with open(env["FAKE_CLAUDE_CALLS"], "a") as fh:
            fh.write(json.dumps(record) + "\n")
    session = env.get("FAKE_CLAUDE_SESSION_ID", "sess-1")
    if "--resume" in argv:
        if env.get("FAKE_CLAUDE_RESUME_FAIL"):
            sys.stderr.write("No conversation found with session ID: x\n")
            sys.exit(1)
        session = argv[argv.index("--resume") + 1]
    key = env.get("ANTHROPIC_API_KEY")
    text = env.get("FAKE_CLAUDE_TEXT", "working on it")
    if env.get("FAKE_CLAUDE_ECHO_KEY"):
        text += f" key={key}"
    assistant = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "session_id": session,
    }
    if env.get("FAKE_CLAUDE_ASSISTANT_FIRST"):
        emit(assistant)
    if not env.get("FAKE_CLAUDE_NO_INIT"):
        emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "apiKeySource": env.get(
                    "FAKE_CLAUDE_API_KEY_SOURCE", "ANTHROPIC_API_KEY" if key else "none"
                ),
                "model": "fake",
                "cwd": os.getcwd(),
            }
        )
    if not env.get("FAKE_CLAUDE_ASSISTANT_FIRST"):
        emit(assistant)
    time.sleep(float(env.get("FAKE_CLAUDE_SLEEP", "0")))
    if not env.get("FAKE_CLAUDE_NO_RESULT"):
        is_error = bool(env.get("FAKE_CLAUDE_IS_ERROR"))
        result = {
            "type": "result",
            "subtype": env.get("FAKE_CLAUDE_SUBTYPE", "success"),
            "is_error": is_error,
            "session_id": session,
            "num_turns": 1,
            "total_cost_usd": "__COST__",
            "result": env.get("FAKE_CLAUDE_ERROR_TEXT", "boom") if is_error else "done",
        }
        line = json.dumps(result).replace('"__COST__"', env.get("FAKE_CLAUDE_COST", "0.0123"))
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    sys.exit(int(env.get("FAKE_CLAUDE_EXIT", "0")))


main()
