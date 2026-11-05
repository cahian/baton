"""Stand-in for the `agy` CLI. Never talks to a vendor.

Stdin must be closed: the fake waits up to two seconds for it to report EOF and exits 3 if it
yields data, 4 if it stays open and silent. That is how the tests prove the adapter closes it.

Limit reads (`-p /usage` and `-p /credits`, text output) print the file named by
`FAKE_AGY_USAGE` / `FAKE_AGY_CREDITS`. `FAKE_AGY_READ_MODE` (when set) is "hang", "flood" or
"fail" and replaces the answer. `FAKE_AGY_READ_LOG` collects one line per read.

A prompt (`-p <text> --output-format stream-json ...`) prints JSON lines. Every call is appended to
`FAKE_AGY_CALLS` (argv and cwd, one JSON object per line). Knobs:
  FAKE_AGY_CONVERSATION_ID  id announced in the init event (default conv-1; the id passed with
                            --conversation wins when present)
  FAKE_AGY_TEXT             text of the agent's text_delta (default "working on it")
  FAKE_AGY_EXEC_SLEEP       seconds to stay alive before the result event
  FAKE_AGY_EXEC_EXIT        exit code (default 0)
  FAKE_AGY_RESULT_STATUS    SUCCESS (default) or ERROR
  FAKE_AGY_RESULT_ERROR     error text of an ERROR result
  FAKE_AGY_NO_RESULT        when set, no result event is printed
  FAKE_AGY_RAW_LINE         a non-JSON line printed before the result
  FAKE_AGY_PIDFILE          gets the pid
  FAKE_AGY_EXTRA            a JSON list of events printed after the first step_update
"""

import json
import os
import select
import sys
import time
from pathlib import Path


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def check_stdin() -> None:
    ready, _, _ = select.select([sys.stdin], [], [], 2)
    if not ready:
        sys.exit(4)  # open and silent: the adapter left stdin attached
    if sys.stdin.read():
        sys.exit(3)


def read_cmd(command: str) -> None:
    log = os.environ.get("FAKE_AGY_READ_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(command + "\n")
    mode = os.environ.get("FAKE_AGY_READ_MODE")
    if mode == "hang":
        time.sleep(600)
    if mode == "flood":
        burst = "Gemini Models\tWeekly Limit Remaining\t100%\t2030-01-01T00:00:00Z\n" * 500
        while True:
            sys.stdout.write(burst)
            sys.stdout.flush()
    if mode == "fail":
        sys.stderr.write(f"error: {command} failed\n")
        sys.exit(1)
    var = "FAKE_AGY_USAGE" if command == "/usage" else "FAKE_AGY_CREDITS"
    sys.stdout.write(Path(os.environ[var]).read_text())
    sys.stdout.flush()


def prompt_cmd(argv: list[str]) -> None:
    calls = os.environ.get("FAKE_AGY_CALLS")
    if calls:
        with open(calls, "a") as fh:
            fh.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    conversation = os.environ.get("FAKE_AGY_CONVERSATION_ID", "conv-1")
    if "--conversation" in argv:
        conversation = argv[argv.index("--conversation") + 1]
    emit({"event": "init", "init": {"conversation_id": conversation, "model": "fake", "cwd": "."}})
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conversation,
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": os.environ.get("FAKE_AGY_TEXT", "working on it"),
            },
        }
    )
    for extra in json.loads(os.environ.get("FAKE_AGY_EXTRA", "[]")):
        emit(extra)
    raw = os.environ.get("FAKE_AGY_RAW_LINE")
    if raw:
        sys.stdout.write(raw + "\n")
        sys.stdout.flush()
    time.sleep(float(os.environ.get("FAKE_AGY_EXEC_SLEEP", "0")))
    if not os.environ.get("FAKE_AGY_NO_RESULT"):
        status = os.environ.get("FAKE_AGY_RESULT_STATUS", "SUCCESS")
        result = {"conversation_id": conversation, "status": status, "response": "done\n"}
        if status == "SUCCESS":
            result["num_turns"] = 1
        else:
            result["conversation_id"] = ""
            result["response"] = ""
            result["error"] = os.environ.get("FAKE_AGY_RESULT_ERROR", "boom")
        emit({"event": "result", "result": result})
    sys.exit(int(os.environ.get("FAKE_AGY_EXEC_EXIT", "0")))


def main() -> None:
    pidfile = os.environ.get("FAKE_AGY_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()))
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] != "-p":
        sys.exit(2)
    check_stdin()
    if argv[1] in ("/usage", "/credits"):
        read_cmd(argv[1])
    else:
        prompt_cmd(argv)


main()
