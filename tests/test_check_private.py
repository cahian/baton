"""The private-data scanner. Samples are assembled at run time so this file never matches the
scanner it tests."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location("check_private", ROOT / "scripts/check_private.py")
check_private = importlib.util.module_from_spec(SPEC)
sys.modules["check_private"] = check_private
SPEC.loader.exec_module(check_private)


def classes(text: str) -> set[str]:
    return {f.kind for f in check_private.scan_text(text, "sample.txt")}


SAMPLES = {
    "email": "contact " + "jane.doe" + "@" + "corp-mail" + ".com",
    "home path": "see " + "/" + "Users" + "/" + "jane" + "/work",
    "home path (linux)": "see " + "/" + "home" + "/" + "jane" + "/work",
    "private ip": "host " + ".".join(["10", "1", "2", "3"]),
    "private ip (172 range)": "host " + ".".join(["172", "20", "0", "9"]),
    "private ip (192 range)": "host " + ".".join(["192", "168", "1", "77"]),
    "tailscale ip": "host " + ".".join(["100", "101", "102", "103"]),
    "tailscale hostname": "ssh box." + "tail1234" + "." + "ts" + "." + "net",
    "uuid": "id " + "-".join(["123e4567", "e89b", "12d3", "a456", "426614174000"]),
    "openai-style token": "key " + "sk" + "-" + "A1b2C3d4E5f6G7h8",
    "github token": "tok " + "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "google oauth token": "tok " + "4/" + "0A" + "bcdEFGhijKLMnopQRstuVWxyz01",
    "tailscale ipv6": "host " + "fd7a" + ":115c:" + "a1e0::1",
    "tailscale hostname (upper case)": "ssh BOX." + "TAIL1234" + "." + "TS" + "." + "NET",
    "home path (windows)": "see C:" + "\\" + "Users" + "\\" + "jane" + "\\work",
    "home path (windows, escaped)": "see C:" + "\\\\" + "Users" + "\\\\" + "jane",
    "aws token": "id " + "AK" + "IA" + "ABCDEFGHIJKLMNOP",
}


@pytest.mark.parametrize("kind", SAMPLES)
def test_detects_each_class(kind):
    expected = {"tailscale ipv6": "tailscale ip"}.get(kind, kind.split(" (")[0])
    expected = "token" if kind.endswith("token") else expected
    assert expected in classes(SAMPLES[kind])


@pytest.mark.parametrize("name", ["runner", "username", "you", "example", "jane"])
def test_only_user_is_a_home_placeholder(name):
    for text in ("/" + "Users" + "/" + name, "/" + "home" + "/" + name):
        assert classes(text) == {"home path"}, text
    assert classes("C:" + "\\" + "Users" + "\\" + name) == {"home path"}


@pytest.mark.parametrize(
    "text",
    ["/" + "Users" + "/user/x", "/" + "home" + "/user", "C:" + "\\" + "Users" + "\\<name>"],
)
def test_user_and_angle_brackets_stay_allowed(text):
    assert classes(text) == set()


def test_detects_each_class_by_name():
    found = {check_private.scan_text(text, "x")[0].kind for text in SAMPLES.values()}
    assert found == {
        "email",
        "home path",
        "private ip",
        "tailscale ip",
        "tailscale hostname",
        "uuid",
        "token",
    }


@pytest.mark.parametrize(
    "text",
    [
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
        "Claude-Session: https://claude.ai/code/session_01EgGzjmFAvVGv8vf25aLJ7B",
        "cd /path/to/project",
        "workdir = " + '"/' + "Users" + "/<name>/project" + '"',
        "key_file = ~/.config/baton/anthropic.key",
        "state in ~/.local/state/baton",
        "bind 127.0.0.1 only",
        "a public address 8.8.8.8",
        "mail someone@example.com",
        "fixture baton-tests@baton.invalid",
        "task-9-brief and risk-adjusted",
        "version 1.2.3.4 of a thing",
        "a decorator @pytest.mark.parametrize",
        "172.32.0.1 is public",
    ],
)
def test_allows_placeholders(text):
    assert check_private.scan_text(text, "sample.txt") == []


def test_a_finding_masks_the_match():
    (finding,) = check_private.scan_text(SAMPLES["github token"], "sample.txt")
    assert finding.line == 1
    assert "A1b2C3d4E5f6G7h8I9j0K1l2M3" not in finding.excerpt
    assert "A1b2C3d4E5f6G7h8I9j0K1l2M3" not in str(finding)


def test_scan_text_reports_line_numbers():
    text = "clean\nclean\n" + SAMPLES["email"] + "\n"
    (finding,) = check_private.scan_text(text, "f.md")
    assert (finding.path, finding.line) == ("f.md", 3)


def test_commit_trailers_are_allowed_but_other_text_is_not():
    ok = (
        "chore: x\n\nbody\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
        "Claude-Session: https://claude.ai/code/session_01EgGzjmFAvVGv8vf25aLJ7B\n"
    )
    assert check_private.scan_text(ok, "commit") == []
    bad = ok + "Signed-off-by: " + SAMPLES["email"] + "\n"
    assert [f.kind for f in check_private.scan_text(bad, "commit")] == ["email"]


def git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


def make_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    return tmp_path


def test_scan_repo_reads_tracked_files_only(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "tracked.txt").write_text(SAMPLES["email"] + "\n")
    (repo / "untracked.txt").write_text(SAMPLES["email"] + "\n")
    git(repo, "add", "tracked.txt")
    assert {f.path for f in check_private.scan_repo(repo)} == {"tracked.txt"}


def test_scan_repo_reads_files_that_are_not_utf8(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "latin1.txt").write_bytes(
        "caf\xe9 ".encode("latin-1") + SAMPLES["email"].encode("latin-1") + b"\n"
    )
    git(repo, "add", "latin1.txt")
    assert [(f.path, f.kind) for f in check_private.scan_repo(repo)] == [("latin1.txt", "email")]


def test_scan_repo_skips_binary_files(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\xff\xfe\x00" + SAMPLES["email"].encode())
    git(repo, "add", "blob.bin")
    assert check_private.scan_repo(repo) == []


def test_scan_commits_reads_every_message(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a").write_text("a")
    git(repo, "add", "a")
    git(repo, "commit", "-q", "-m", "first")
    (repo / "b").write_text("b")
    git(repo, "add", "b")
    git(repo, "commit", "-q", "-m", "second " + SAMPLES["email"])
    findings = check_private.scan_commits(repo)
    assert [f.kind for f in findings] == ["email"]
    assert findings[0].path.startswith("commit ")


def test_main_exits_non_zero_on_a_find(tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text(SAMPLES["tailscale hostname"] + "\n")
    git(repo, "add", "a.txt")
    assert check_private.main(["--root", str(repo), "--no-commits"]) == 1
    out = capsys.readouterr().out
    assert "a.txt:1" in out and "tailscale hostname" in out


def test_main_exits_zero_when_clean(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("nothing here\n")
    git(repo, "add", "a.txt")
    assert check_private.main(["--root", str(repo), "--no-commits"]) == 0


def tracked() -> list[str]:
    return subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")


def test_repo_is_clean():
    findings = check_private.scan_repo(ROOT) + check_private.scan_commits(ROOT)
    assert findings == [], "\n".join(str(f) for f in findings)


def test_working_notes_are_never_tracked():
    paths = tracked()
    assert not [p for p in paths if p.startswith(".superpowers/")]
    assert ".oneshot-state.md" not in paths
    for path in (".superpowers/x", ".oneshot-state.md"):
        ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
        assert ignored.returncode == 0, f"{path} is not ignored"
