"""Fail when tracked files or commit messages hold personal data.

The repository must carry no e-mail address, home path, private address, hostname on a tailnet,
identifier or credential. This scans what git tracks and every commit message, and prints where
each find is (never the find itself, only a masked excerpt). Exit 1 on any find.

The patterns are written so that this file does not match them.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

OCTET = r"\d{1,3}"
NOT_PART_OF_A_NUMBER = r"(?![\d]|\.\d)"
BEFORE_ADDRESS = r"(?<![\d.])"

# (kind, pattern). Order matters only for the report.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    (
        "home path",
        re.compile(r"(?<![\w.~-])/(?:Users|home)/([^\s/\"'`<>$(){}\[\]*|\\]+)"),
    ),
    (
        "private ip",
        re.compile(
            BEFORE_ADDRESS
            + "(?:"
            + rf"10\.{OCTET}\.{OCTET}\.{OCTET}"
            + rf"|172\.(?:1[6-9]|2\d|3[01])\.{OCTET}\.{OCTET}"
            + rf"|192\.168\.{OCTET}\.{OCTET}"
            + ")"
            + NOT_PART_OF_A_NUMBER
        ),
    ),
    (
        "tailscale ip",
        re.compile(
            BEFORE_ADDRESS
            + rf"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.{OCTET}\.{OCTET}"
            + NOT_PART_OF_A_NUMBER
        ),
    ),
    ("tailscale hostname", re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net\b")),
    (
        "uuid",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
    ),
    (
        "token",
        re.compile(
            r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}"
            r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
            r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
            r"|(?<![\w/])4/0A[\w-]{20,}"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bAIza[\w-]{30,}"
            r"|\bxox[abpr]-[A-Za-z0-9-]{10,}"
        ),
    ),
]

ALLOWED_EMAILS = {"noreply@anthropic.com"}
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}
# Top-level domains reserved for documentation and tests (RFC 2606): no one can own them.
RESERVED_TLDS = {"invalid", "test", "example", "localhost"}
# Names after /Users/ or /home/ that stand for "whoever you are" and identify nobody.
ALLOWED_HOME_NAMES = {"user", "username", "you", "example", "runner"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.excerpt}"


def _mask(value: str) -> str:
    return value[:2] + "***"


def _allowed(kind: str, match: re.Match[str]) -> bool:
    if kind == "email":
        address = match.group(0).lower()
        domain = address.rsplit("@", 1)[1]
        return (
            address in ALLOWED_EMAILS
            or domain in ALLOWED_EMAIL_DOMAINS
            or domain.rsplit(".", 1)[1] in RESERVED_TLDS
        )
    if kind == "home path":
        return match.group(1).lower() in ALLOWED_HOME_NAMES
    return False


def scan_text(text: str, where: str) -> list[Finding]:
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if not _allowed(kind, match):
                    found.append(Finding(where, number, kind, _mask(match.group(0))))
    return found


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True, stdin=subprocess.DEVNULL
    ).stdout


def scan_repo(root: Path) -> list[Finding]:
    """Every tracked file that is text. Binary files and files gone from disk are skipped."""
    found: list[Finding] = []
    for name in _git(root, "ls-files", "-z").decode("utf-8").split("\0"):
        if not name:
            continue
        try:
            raw = (root / name).read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.extend(scan_text(text, name))
    return found


def scan_commits(root: Path) -> list[Finding]:
    """Every commit message reachable from HEAD. The trailers baton's commits carry are allowed
    because their e-mail and session URL match no pattern above or are in the allow lists."""
    out = _git(root, "log", "--format=%H%x00%B%x01").decode("utf-8", errors="replace")
    found: list[Finding] = []
    for entry in out.split("\x01"):
        if "\0" not in entry:
            continue
        sha, message = entry.strip("\n").split("\0", 1)
        found.extend(scan_text(message, f"commit {sha[:10]}"))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on personal data in tracked files.")
    parser.add_argument("--root", default=".", help="repository root (default: the current one)")
    parser.add_argument("--no-commits", action="store_true", help="skip commit messages")
    args = parser.parse_args(argv)
    root = Path(args.root)
    findings = scan_repo(root)
    if not args.no_commits:
        findings += scan_commits(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} possible private value(s) found", file=sys.stderr)
        return 1
    print("no private data found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
