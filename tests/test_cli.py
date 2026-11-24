"""The fantasygolf command line: version, unknown commands, console script, path config."""

import re
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from fantasygolf.cli import main
from fantasygolf.config import Paths


def run_main(argv):
    """Call main(argv); return its exit code whether it returns or raises SystemExit."""
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    return 0 if code is None else code


def test_version_flag(capsys):
    assert run_main(["--version"]) == 0
    assert capsys.readouterr().out == "fantasygolf 0.1.0\n"


def test_help_flag_prints_usage_and_exits_0(capsys):
    assert run_main(["--help"]) == 0
    assert capsys.readouterr().out.startswith("usage:")


@pytest.mark.parametrize("name", ["nosuch", "scrape-everything", "Predict", "--nosuchflag", "x"])
def test_unknown_command_exits_2(name, capsys):
    with pytest.raises(SystemExit) as info:
        main([name])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage:")
    assert name in err


def test_console_script_is_registered():
    eps = [ep for ep in entry_points(group="console_scripts") if ep.name == "fantasygolf"]
    assert [ep.value for ep in eps] == ["fantasygolf.cli:main"]


@pytest.mark.parametrize(
    ("args", "code", "out", "err"),
    [
        (["--version"], 0, "fantasygolf 0.1.0\n", ""),
        (["nosuch"], 2, "", "usage:"),
    ],
)
def test_installed_console_script_runs(args, code, out, err):
    script = Path(sys.executable).parent / "fantasygolf"
    assert script.exists(), f"console script {script} is not installed"
    proc = subprocess.run([str(script), *args], capture_output=True, text=True, timeout=60)
    assert proc.returncode == code
    assert proc.stdout == out
    assert err in proc.stderr


ENV_VARS = ("FANTASYGOLF_DB", "FANTASYGOLF_CACHE", "FANTASYGOLF_REPORTS")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return Path.cwd()


def test_paths_defaults_are_under_working_directory(clean_env):
    paths = Paths.from_env()
    assert isinstance(paths, Paths)
    assert paths.db.resolve() == clean_env / "data" / "fantasygolf.duckdb"
    assert paths.cache.resolve() == clean_env / "data" / "raw"
    assert paths.reports.resolve() == clean_env / "reports"


def test_paths_defaults_follow_the_working_directory(clean_env, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert Paths.from_env().db.resolve() == Path.cwd() / "data" / "fantasygolf.duckdb"
    assert Paths.from_env().db.resolve() != clean_env / "data" / "fantasygolf.duckdb"


@pytest.mark.parametrize(
    ("var", "field", "default_rel"),
    [
        ("FANTASYGOLF_DB", "db", "data/fantasygolf.duckdb"),
        ("FANTASYGOLF_CACHE", "cache", "data/raw"),
        ("FANTASYGOLF_REPORTS", "reports", "reports"),
    ],
)
def test_paths_env_override(var, field, default_rel, clean_env, monkeypatch, tmp_path):
    target = tmp_path / "custom" / "place"
    monkeypatch.setenv(var, str(target))
    paths = Paths.from_env()
    assert isinstance(getattr(paths, field), Path)
    assert getattr(paths, field) == target
    # The other two fields keep their defaults.
    defaults = {
        "db": clean_env / "data" / "fantasygolf.duckdb",
        "cache": clean_env / "data" / "raw",
        "reports": clean_env / "reports",
    }
    for other, expected in defaults.items():
        if other != field:
            assert getattr(paths, other).resolve() == expected


def test_paths_all_overrides_together(clean_env, monkeypatch, tmp_path):
    values = {
        "FANTASYGOLF_DB": tmp_path / "a" / "x.duckdb",
        "FANTASYGOLF_CACHE": tmp_path / "b",
        "FANTASYGOLF_REPORTS": tmp_path / "c" / "d",
    }
    for var, value in values.items():
        monkeypatch.setenv(var, str(value))
    assert Paths.from_env() == Paths(
        db=values["FANTASYGOLF_DB"],
        cache=values["FANTASYGOLF_CACHE"],
        reports=values["FANTASYGOLF_REPORTS"],
    )


def test_version_text_matches_package_version(capsys):
    import fantasygolf

    assert re.fullmatch(r"\d+\.\d+\.\d+", fantasygolf.__version__)
    run_main(["--version"])
    assert capsys.readouterr().out == f"fantasygolf {fantasygolf.__version__}\n"
