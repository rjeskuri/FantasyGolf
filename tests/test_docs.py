"""Project docs are filled in: no template placeholders, README lists the commands."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_claude_md_has_no_placeholders():
    text = (ROOT / "CLAUDE.md").read_text()
    assert re.findall(r"<[^<>\n]+>", text) == []


def test_readme_has_commands_table():
    readme = ROOT / "README.md"
    assert readme.exists()
    text = readme.read_text()
    assert re.search(r"^\|.*\|\s*$\n^\|\s*:?-{3,}", text, re.MULTILINE), "no markdown table"
    assert "fantasygolf" in text
