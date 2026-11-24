<!--
Maintainer notes (HTML comments are stripped before Claude sees this file, so they cost no context).
Scaffolded by harness-scaffold (python, 2026). Keep it short: only what Claude cannot infer
from the code. The loop's rules (board, branches, tests first) live in ~/.claude/CLAUDE.md and the
hooks, not here. When Claude makes the same mistake twice, add one line here.
-->

# FantasyGolf

A local pipeline that scrapes pgatour.com history into DuckDB and a raw JSON cache, builds features and projects next-year earnings as a distribution for a tiered fantasy golf league (CLI commands predict, trade-check, backtest); correct means projections reproduce from the cached data, and tests run on recorded fixtures only.

## Commands
- Verify, the whole suite plus lint (CI runs the same): `scripts/verify.sh`
- Named test files: `uv run pytest -q {files}`, with `{files}` replaced by their paths

## Where things live
- `tests/`: the test suite.
- `scripts/verify.sh`: the checks behind the verify command; `.github/workflows/ci.yml` runs it.
- `.githooks/`: `pre-commit` (format and lint on staged files, no commits on main) and `pre-push` (no pushes to main, no force pushes).
- `src/fantasygolf/cli.py`: the `fantasygolf` command; subcommands register as entries in `COMMANDS`.
- `src/fantasygolf/config.py`: `Paths.from_env()` for the database, cache and reports locations.
- `data/raw/`: raw JSON cache (ignored); `data/manual/`: hand-maintained data (tracked); `reports/`: generated output (ignored).

## Gotchas
- Tests use recorded fixtures only; `tests/conftest.py` blocks every non-loopback network connection.
- The live scrape is manual, never run by tests or CI.
- Paths default under the working directory; override with `FANTASYGOLF_DB`, `FANTASYGOLF_CACHE`, `FANTASYGOLF_REPORTS`.
