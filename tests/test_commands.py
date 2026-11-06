"""`baton:` commands: the person's messages that steer a job instead of being work."""

import pytest

from baton.commands import Command, parse_command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("baton: goal ship the parser", Command("goal", "ship the parser")),
        ("BATON: Goal Ship it", Command("goal", "Ship it")),
        ("  baton:goal   padded  \n", Command("goal", "padded")),
        (
            "baton: goal first line\nsecond line\n\nthird",
            Command("goal", "first line\nsecond line\n\nthird"),
        ),
        ("baton: goal\nstarts on the next line", Command("goal", "starts on the next line")),
        ("baton: goal off", Command("goal_off")),
        ("Baton: GOAL OFF  ", Command("goal_off")),
        ("baton: goal off the lights", Command("goal", "off the lights")),
        ("baton: next", Command("next")),
        ("baton: NEXT now", Command("next", now=True)),
        ("baton: next   NOW \n", Command("next", now=True)),
    ],
)
def test_a_command_is_recognised(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello",
        "baton: goal",  # the goal text is required
        "baton: goal   \n ",
        "baton: next please",
        "baton: next now please",
        "baton: nextnow",
        "baton: goaloff",
        "baton: stop",
        "baton:",
        "please set baton: goal x",  # it must start the message
        "/baton: goal x",
        "the baton: next is a relay thing",
        "batonx: goal x",
    ],
)
def test_anything_else_is_an_ordinary_message(text):
    assert parse_command(text) is None
