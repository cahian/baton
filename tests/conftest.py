"""Shared fixtures: fake `codex` and `agy` executables on PATH, and an isolated state dir."""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baton.proc import SCRUBBED_ENV

FAKES = Path(__file__).parent / "fakes"
NOW_FAR = 4_000_000_000.0  # year 2096: a reset that is always in the future


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def codex_limits(primary=12, secondary=31, balance="1000.5000000000", reset=NOW_FAR):
    """A successful `account/rateLimits/read` result (reset far in the future)."""
    window = {"windowDurationMins": 300, "resetsAt": reset}
    return {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "primary": None if primary is None else {"usedPercent": primary, **window},
            "secondary": None
            if secondary is None
            else {"usedPercent": secondary, **window, "windowDurationMins": 10080},
            "credits": {"hasCredits": True, "unlimited": False, "balance": balance},
            "rateLimitReachedType": None,
            "planType": "plus",
        },
    }


def agy_usage(five=100, weekly=100, reset=NOW_FAR):
    """A `/usage` text; the numbers are percent REMAINING."""
    return (
        f"Gemini Models\tWeekly Limit Remaining\t{weekly}%\t{iso(reset)}\n"
        f"Gemini Models\tFive Hour Limit Remaining\t{five}%\t{iso(reset)}\n"
        f"Claude and GPT models\tWeekly Limit Remaining\t100%\t{iso(reset)}\n"
        f"Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t99%\t{iso(reset)}\n"
    )


def agy_credits(balance="0") -> str:
    return f"Remaining credits\t{balance}\n"


@pytest.fixture(autouse=True)
def isolated_baton_files(tmp_path, monkeypatch):
    """No test reads the real defaults file or touches the real spend ledger."""
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "config" / "defaults.toml"))
    monkeypatch.setenv("BATON_LEDGER", str(tmp_path / "ledger" / "claude-spend.json"))


class Fakes:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bindir = root / "bin"
        self.codex_scenario_file = root / "codex-scenario.json"
        self.codex_calls_file = root / "codex-calls.jsonl"
        self.codex_pidfile = root / "codex-exec.pid"
        self.agy_calls_file = root / "agy-calls.jsonl"
        self.claude_calls_file = root / "claude-calls.jsonl"
        self.claude_key_file = root / "anthropic.key"
        self.agy_read_log = root / "agy-reads.log"
        self.agy_usage_file = root / "agy-usage.txt"
        self.agy_credits_file = root / "agy-credits.txt"
        self.state = root / "state"
        self.ledger_file = root / "ledger" / "claude-spend.json"
        self.defaults_file = root / "config" / "defaults.toml"
        self.work = root / "work"

    def codex_scenario(self, *entries) -> None:
        self.codex_scenario_file.write_text(json.dumps(list(entries)))
        Path(str(self.codex_scenario_file) + ".cursor").unlink(missing_ok=True)

    def agy_files(self, usage: str | None = None, credits: str | None = None) -> None:
        self.agy_usage_file.write_text(agy_usage() if usage is None else usage)
        self.agy_credits_file.write_text(agy_credits() if credits is None else credits)

    @staticmethod
    def _lines(path: Path) -> list:
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    def codex_calls(self) -> list:
        return self._lines(self.codex_calls_file)

    def agy_calls(self) -> list:
        return self._lines(self.agy_calls_file)

    def claude_calls(self) -> list:
        return self._lines(self.claude_calls_file)

    def defaults(self, text: str) -> None:
        self.defaults_file.parent.mkdir(parents=True, exist_ok=True)
        self.defaults_file.write_text(text)

    def claude_key(self, key: str, mode: int = 0o600) -> None:
        self.claude_key_file.write_text(key + "\n")
        self.claude_key_file.chmod(mode)

    def goal(
        self,
        name="demo",
        verify=None,
        extra="",
        poll_s=1,
        codex="",
        agy="",
        claude=None,
        order=("codex", "antigravity"),
    ) -> Path:
        """Write a goal file for the work directory and return its path.

        Claude is off unless `claude` gives its settings: a test must opt in to it, and then it
        uses the fake key file of this fixture."""
        lines = [
            f'name = "{name}"',
            f'workdir = "{self.work}"',
            'objective = "Make the widget frobnicate."',
            "snapshot = false",
            f"order = {json.dumps(list(order))}",
            extra,
        ]
        if verify:
            lines += ["[verify]", f"command = '{verify}'"]
        lines += [
            f"[agents.codex]\npoll_s = {poll_s}\n{codex}",
            f"[agents.antigravity]\npoll_s = {poll_s}\n{agy}",
            "[agents.claude_api]\n"
            + (
                f'key_file = "{self.claude_key_file}"\n{claude}'
                if claude is not None
                else "enabled = false"
            ),
        ]
        path = self.root / "goal.toml"
        path.write_text("\n".join(lines) + "\n")
        return path


@pytest.fixture
def fakes(tmp_path, monkeypatch) -> Fakes:
    """Fake `codex` and `agy` first on PATH, with every knob pointed into tmp_path."""
    f = Fakes(tmp_path)
    f.bindir.mkdir()
    f.work.mkdir()
    for name, script in (
        ("codex", "fake_codex.py"),
        ("agy", "fake_agy.py"),
        ("claude", "fake_claude.py"),
    ):
        wrapper = f.bindir / name
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / script}" "$@"\n')
        wrapper.chmod(0o755)
    setenv = monkeypatch.setenv
    for var in SCRUBBED_ENV:
        monkeypatch.delenv(var, raising=False)
    setenv("PATH", f"{f.bindir}{os.pathsep}{os.environ['PATH']}")
    setenv("BATON_STATE_DIR", str(f.state))
    setenv("FAKE_CODEX_SCENARIO", str(f.codex_scenario_file))
    setenv("FAKE_CODEX_CALLS", str(f.codex_calls_file))
    setenv("FAKE_CODEX_EXEC_PIDFILE", str(f.codex_pidfile))
    setenv("HOME", str(tmp_path / "home"))  # no test reads the real home, or a real key file
    setenv("FAKE_CLAUDE_CALLS", str(f.claude_calls_file))
    setenv("FAKE_AGY_CALLS", str(f.agy_calls_file))
    setenv("FAKE_AGY_READ_LOG", str(f.agy_read_log))
    setenv("FAKE_AGY_USAGE", str(f.agy_usage_file))
    setenv("FAKE_AGY_CREDITS", str(f.agy_credits_file))
    f.codex_scenario(codex_limits())
    f.agy_files()
    return f
