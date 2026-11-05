"""`baton install-service`, with a stub `launchctl`: no test touches the real launchd."""

import os
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from baton import service
from baton.cli import main
from baton.gateway import SHUTDOWN_S
from baton.store import state_base

LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$STUB_CALLS"
case "$1" in
  print) [ -f "$STUB_LOADED" ] ;;
  bootout) : ;;
  bootstrap)
    if [ -f "$STUB_FAILS" ]; then
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    : > "$STUB_LOADED" ;;
esac
"""


def executable(path: Path, text="#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class World:
    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.calls_file = root / "launchctl-calls"
        self.fails = root / "launchctl-fails"
        self.monkeypatch = monkeypatch
        self.tools = root / "tools"
        self.home = root / "home"
        self.home.mkdir()
        executable(self.tools / "launchctl", LAUNCHCTL)
        executable(root / "codex-dir/codex")
        executable(root / "agy-dir/agy")
        executable(root / "claude-dir/claude")
        executable(root / "node-dir/node")
        self.path(self.tools, "codex-dir", "agy-dir", "claude-dir", "node-dir")
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("STUB_CALLS", str(self.calls_file))
        monkeypatch.setenv("STUB_LOADED", str(root / "launchctl-loaded"))
        monkeypatch.setenv("STUB_FAILS", str(self.fails))
        monkeypatch.setattr(service, "BOOTSTRAP_PAUSE_S", 0)

    def path(self, *dirs) -> None:
        self.monkeypatch.setenv("PATH", os.pathsep.join(str(self.root / d) for d in dirs))

    @property
    def calls(self) -> list[str]:
        return self.calls_file.read_text().splitlines() if self.calls_file.exists() else []

    @property
    def verbs(self) -> list[str]:
        return [line.split()[0] for line in self.calls]

    @property
    def plist_file(self) -> Path:
        return self.home / "Library/LaunchAgents" / f"{service.LABEL}.plist"

    def plist(self) -> dict:
        return plistlib.loads(self.plist_file.read_bytes())


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_install_writes_the_plist_and_loads_it(world, capsys):
    assert main(["install-service", "--port", "19000"]) == 0
    plist = world.plist()
    assert plist["Label"] == service.LABEL
    assert world.plist_file.name == f"{plist['Label']}.plist"
    assert plist["ProgramArguments"][-3:] == ["serve", "--port", "19000"]
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    domain = f"gui/{os.getuid()}"
    assert world.calls[-1] == f"bootstrap {domain} {world.plist_file}"
    assert "installed and loaded" in capsys.readouterr().out


def test_the_default_port_is_the_gateway_s(world):
    assert main(["install-service"]) == 0
    assert world.plist()["ProgramArguments"][-1] == "18777"


def test_every_path_in_the_plist_is_absolute(world):
    assert main(["install-service"]) == 0
    plist = world.plist()
    assert Path(plist["ProgramArguments"][0]).is_absolute()
    assert Path(plist["StandardOutPath"]).is_absolute()
    assert Path(plist["StandardErrorPath"]).is_absolute()
    assert all(Path(d).is_absolute() for d in plist["EnvironmentVariables"]["PATH"].split(":"))


def test_path_reaches_every_agent_and_node(world):
    assert main(["install-service"]) == 0
    dirs = world.plist()["EnvironmentVariables"]["PATH"].split(":")
    for name in ("codex-dir", "agy-dir", "claude-dir", "node-dir"):
        assert str(world.root / name) in dirs
    assert "/usr/bin" in dirs and "/bin" in dirs  # sh, ps and git
    assert len(dirs) == len(set(dirs))


def test_the_baton_on_path_is_what_runs(world):
    executable(world.root / "baton-dir/baton")
    world.path("tools", "codex-dir", "agy-dir", "claude-dir", "node-dir", "baton-dir")
    assert main(["install-service"]) == 0
    assert world.plist()["ProgramArguments"][0] == str(world.root / "baton-dir/baton")


def test_without_baton_on_path_this_interpreter_runs_it(world):
    assert main(["install-service"]) == 0
    assert world.plist()["ProgramArguments"][:3] == [sys.executable, "-m", "baton.cli"]


def test_launchd_gives_the_shutdown_time_to_finish(world):
    assert main(["install-service"]) == 0
    assert world.plist()["ExitTimeOut"] > SHUTDOWN_S


def test_logs_go_under_the_state_directory_which_exists(world):
    assert main(["install-service"]) == 0
    plist = world.plist()
    assert Path(plist["StandardOutPath"]).parent == state_base()
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]
    assert state_base().is_dir()


def test_the_service_keeps_the_shell_s_state_directory(world, monkeypatch):
    monkeypatch.setenv("BATON_STATE_DIR", str(world.root / "elsewhere"))
    assert main(["install-service"]) == 0
    environment = world.plist()["EnvironmentVariables"]
    assert environment["BATON_STATE_DIR"] == str(world.root / "elsewhere")
    assert environment["HOME"] == str(world.home)


def memory_installed(world, monkeypatch):
    executable(world.root / "memory-dir/ai-memory")
    world.path("tools", "codex-dir", "agy-dir", "claude-dir", "node-dir", "memory-dir")
    monkeypatch.delenv("BATON_MEMORY", raising=False)


def test_path_reaches_ai_memory_when_it_is_installed(world, monkeypatch):
    memory_installed(world, monkeypatch)
    assert main(["install-service"]) == 0
    dirs = world.plist()["EnvironmentVariables"]["PATH"].split(":")
    assert str(world.root / "memory-dir") in dirs
    assert len(dirs) == len(set(dirs))


def test_path_reaches_the_ai_memory_a_bin_setting_names(world, monkeypatch):
    executable(world.root / "elsewhere/my-memory")
    world.path("tools", "codex-dir", "agy-dir", "claude-dir", "node-dir", "elsewhere")
    monkeypatch.delenv("BATON_MEMORY", raising=False)
    config = world.root / "defaults.toml"
    config.write_text('[memory]\nbin = "my-memory"\n')
    monkeypatch.setenv("BATON_CONFIG", str(config))
    assert main(["install-service"]) == 0
    assert str(world.root / "elsewhere") in world.plist()["EnvironmentVariables"]["PATH"].split(":")


def test_without_ai_memory_the_path_is_what_it_was(world, monkeypatch):
    monkeypatch.delenv("BATON_MEMORY", raising=False)
    assert main(["install-service"]) == 0
    dirs = world.plist()["EnvironmentVariables"]["PATH"].split(":")
    assert str(world.root / "memory-dir") not in dirs and len(dirs) == 8


def test_memory_that_is_off_adds_nothing_to_the_path(world, monkeypatch):
    memory_installed(world, monkeypatch)
    monkeypatch.setenv("BATON_MEMORY", "off")
    assert main(["install-service"]) == 0
    assert str(world.root / "memory-dir") not in world.plist()["EnvironmentVariables"]["PATH"]


def test_broken_memory_settings_do_not_stop_the_install(world, monkeypatch):
    memory_installed(world, monkeypatch)
    config = world.root / "defaults.toml"
    config.write_text("[memory]\nport = 5\n")
    monkeypatch.setenv("BATON_CONFIG", str(config))
    assert main(["install-service"]) == 0
    assert str(world.root / "memory-dir") not in world.plist()["EnvironmentVariables"]["PATH"]


def test_the_service_keeps_the_shell_s_memory_switch(world, monkeypatch):
    monkeypatch.setenv("BATON_MEMORY", "off")
    assert main(["install-service"]) == 0
    assert world.plist()["EnvironmentVariables"]["BATON_MEMORY"] == "off"
    assert "BATON_MEMORY" in service.PASSED_ON


def test_installing_twice_replaces_the_loaded_service(world):
    assert main(["install-service"]) == 0
    assert world.verbs == ["print", "bootstrap"]
    assert main(["install-service", "--port", "19000"]) == 0
    assert world.verbs == ["print", "bootstrap", "print", "bootout", "bootstrap"]
    assert world.plist()["ProgramArguments"][-1] == "19000"


def test_a_service_still_leaving_is_waited_for(world, monkeypatch):
    real = service._launchctl
    tries = []

    def launchctl(path, *args):
        if args[0] == "bootstrap":
            tries.append(args)
            if len(tries) == 3:
                world.fails.unlink()
        return real(path, *args)

    monkeypatch.setattr(service, "_launchctl", launchctl)
    world.fails.write_text("")
    assert main(["install-service"]) == 0
    assert len(tries) == 3


def waits(world, monkeypatch):
    """Record the pauses between tries, at their real length, without sleeping them."""
    slept = []
    monkeypatch.setattr(service, "BOOTSTRAP_PAUSE_S", 1.0)
    monkeypatch.setattr(service, "time", SimpleNamespace(sleep=slept.append))
    return slept


def test_a_service_that_takes_its_whole_exit_timeout_is_waited_for(world, monkeypatch):
    assert main(["install-service"]) == 0
    slept = waits(world, monkeypatch)
    real = service._launchctl

    def launchctl(path, *args):
        if args[0] == "bootstrap" and sum(slept) > service.EXIT_TIMEOUT_S:
            world.fails.unlink()
        return real(path, *args)

    monkeypatch.setattr(service, "_launchctl", launchctl)
    world.fails.write_text("")
    assert main(["install-service"]) == 0


def test_a_replacement_that_never_loads_gives_up_after_the_exit_timeout(world, monkeypatch, capsys):
    assert main(["install-service"]) == 0
    slept = waits(world, monkeypatch)
    world.fails.write_text("")
    assert main(["install-service"]) == 1
    assert "Input/output error" in capsys.readouterr().err
    assert service.EXIT_TIMEOUT_S < sum(slept) <= service.EXIT_TIMEOUT_S + 30


def test_with_nothing_leaving_a_failed_load_is_reported_soon(world, monkeypatch):
    slept = waits(world, monkeypatch)
    world.fails.write_text("")
    assert main(["install-service"]) == 1
    assert sum(slept) < 10


def test_a_failed_load_says_why(world, capsys):
    world.fails.write_text("")
    assert main(["install-service"]) == 1
    assert "Input/output error" in capsys.readouterr().err
    assert world.verbs.count("bootstrap") == service.BOOTSTRAP_TRIES


def test_without_launchctl_nothing_is_written(world, capsys):
    world.path("codex-dir", "agy-dir", "claude-dir", "node-dir")
    assert main(["install-service"]) == 1
    assert "launchctl was not found" in capsys.readouterr().err
    assert not world.plist_file.exists()


def test_without_any_agent_nothing_is_written(world, capsys):
    world.path("tools")
    assert main(["install-service"]) == 1
    err = capsys.readouterr().err
    assert "no agent binary" in err and "codex" in err and "agy" in err
    assert not world.plist_file.exists()
    assert world.calls == []


def test_a_missing_agent_is_named_and_the_others_are_installed(world, capsys):
    world.path("tools", "codex-dir", "claude-dir", "node-dir")
    assert main(["install-service"]) == 0
    assert "agy was not found on PATH" in capsys.readouterr().out
    assert str(world.root / "agy-dir") not in world.plist()["EnvironmentVariables"]["PATH"]


def test_a_missing_node_is_named(world, capsys):
    world.path("tools", "codex-dir", "agy-dir", "claude-dir")
    assert main(["install-service"]) == 0
    assert "node: not found" in capsys.readouterr().out


def test_a_disabled_agent_is_not_looked_for(world, tmp_path, monkeypatch, capsys):
    config = tmp_path / "defaults.toml"
    config.write_text("[agents.antigravity]\nenabled = false\n")
    monkeypatch.setenv("BATON_CONFIG", str(config))
    world.path("tools", "codex-dir", "claude-dir", "node-dir")
    assert main(["install-service"]) == 0
    assert "agy" not in capsys.readouterr().out


def test_bin_from_the_defaults_file_with_a_tilde_is_resolved(world, tmp_path, monkeypatch):
    executable(world.home / "tools/my-codex")
    config = tmp_path / "defaults.toml"
    config.write_text('[agents.codex]\nbin = "~/tools/my-codex"\n')
    monkeypatch.setenv("BATON_CONFIG", str(config))
    assert main(["install-service"]) == 0
    assert str(world.home / "tools") in world.plist()["EnvironmentVariables"]["PATH"]


def test_a_broken_defaults_file_is_reported(world, tmp_path, monkeypatch, capsys):
    config = tmp_path / "defaults.toml"
    config.write_text("nonsense =\n")
    monkeypatch.setenv("BATON_CONFIG", str(config))
    assert main(["install-service"]) == 2
    assert not world.plist_file.exists()
