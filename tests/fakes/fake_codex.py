"""Stand-in for the `codex` CLI. Never talks to a vendor.

Subcommands:
  app-server  JSON-RPC over stdio. `FAKE_CODEX_SCENARIO` names a JSON file holding a list; each
              spawn consumes the next entry (the last one repeats). An entry is a limit result
              object, `{"__raw_result__": "<json text>"}` (sent verbatim), or one of the
              strings "hang", "flood", "garbage", "rpc_error". The cursor is kept in
              `<scenario>.cursor`, as every read is a fresh process.
              `FAKE_CODEX_RPC` collects every line received, `FAKE_CODEX_PIDFILE` gets the pid.
  exec        Prints JSONL. Every call is appended to `FAKE_CODEX_CALLS` (one JSON object per line,
              with the argv and the stdin text). `FAKE_CODEX_THREAD_ID` is the thread id to
              announce, `FAKE_CODEX_EXEC_SLEEP` the seconds to stay alive afterwards,
              `FAKE_CODEX_EXEC_EXIT` the exit code, `FAKE_CODEX_EXEC_FAIL` a line to print
              (non-JSON, to stderr-like stdout) before exiting. `FAKE_CODEX_EXEC_PIDFILE` gets
              the exec process's pid.
"""

import json
import os
import sys
import time
from pathlib import Path


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def next_scenario_entry():
    path = Path(os.environ["FAKE_CODEX_SCENARIO"])
    entries = json.loads(path.read_text())
    cursor_file = Path(str(path) + ".cursor")
    index = int(cursor_file.read_text()) if cursor_file.exists() else 0
    cursor_file.write_text(str(index + 1))
    return entries[min(index, len(entries) - 1)]


def app_server() -> None:
    pidfile = os.environ.get("FAKE_CODEX_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()))
    entry = next_scenario_entry()
    if entry == "hang":
        time.sleep(600)
        return
    if entry == "flood":
        # Never answers, never pauses: only a deadline checked on every read can end this.
        burst = '{"method": "noise", "params": {}}\n' * 500
        while True:
            sys.stdout.write(burst)
            sys.stdout.flush()
    if entry == "garbage":
        sys.stdout.write("this is not json\n{broken\n")
        sys.stdout.flush()
        return
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        rpc_log = os.environ.get("FAKE_CODEX_RPC")
        if rpc_log:
            with open(rpc_log, "a") as fh:
                fh.write(raw + "\n")
        msg = json.loads(raw)
        if msg.get("method") == "initialize":
            emit({"id": msg["id"], "result": {"userAgent": "fake-codex"}})
            emit({"method": "some/notification", "params": {}})
        elif msg.get("method") == "account/rateLimits/read":
            if entry == "rpc_error":
                emit({"id": msg["id"], "error": {"code": -32000, "message": "boom"}})
            elif isinstance(entry, dict) and "__raw_result__" in entry:
                # Verbatim JSON text, so a test can send numbers that floats would mangle.
                sys.stdout.write(f'{{"id": {msg["id"]}, "result": {entry["__raw_result__"]}}}\n')
                sys.stdout.flush()
            else:
                emit({"id": msg["id"], "result": entry})
            return


def exec_cmd() -> None:
    pidfile = os.environ.get("FAKE_CODEX_EXEC_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()))
    stdin_text = sys.stdin.read()
    calls = os.environ.get("FAKE_CODEX_CALLS")
    if calls:
        with open(calls, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "argv": sys.argv[1:],
                        "stdin": stdin_text,
                        "cwd": os.getcwd(),
                        "anthropic_env": sorted(k for k in os.environ if k.startswith("ANTHROPIC")),
                    }
                )
                + "\n"
            )
    thread_id = os.environ.get("FAKE_CODEX_THREAD_ID", "thread-1")
    emit({"type": "thread.started", "thread_id": thread_id})
    fail = os.environ.get("FAKE_CODEX_EXEC_FAIL")
    if fail:
        sys.stdout.write(fail + "\n")
        sys.stdout.flush()
    else:
        emit(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "working on it"},
            }
        )
    time.sleep(float(os.environ.get("FAKE_CODEX_EXEC_SLEEP", "0")))
    sys.exit(int(os.environ.get("FAKE_CODEX_EXEC_EXIT", "0")))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(2)
    if sys.argv[1] == "app-server":
        app_server()
    elif sys.argv[1] == "exec":
        exec_cmd()
    else:
        sys.exit(2)


main()
