"""Generated data stays out of git; hand-maintained data does not."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def is_ignored(path: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "data/raw/x.json",
        "data/raw/2027/leaderboard/y.json",
        "data/fantasygolf.duckdb",
        "data/fantasygolf.duckdb.wal",
        "data/other.duckdb",
        "data/tiers/2027.csv",
        "reports/a.md",
        "reports/sub/plot.png",
    ],
)
def test_data_outputs_are_ignored(path):
    assert is_ignored(path)


@pytest.mark.parametrize(
    "path",
    [
        "data/manual/a.csv",
        "data/manual/nested/b.csv",
        "src/fantasygolf/cli.py",
        "tests/test_cli.py",
        "README.md",
    ],
)
def test_tracked_paths_are_not_ignored(path):
    assert not is_ignored(path)
