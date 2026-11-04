import tomllib
from pathlib import Path


def test_there_are_no_runtime_dependencies():
    # The design: standard library only. A dependency here would need its own decision.
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == []


def test_the_baton_script_points_at_main():
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["baton"] == "baton.cli:main"
