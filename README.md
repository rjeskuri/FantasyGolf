# FantasyGolf

A local pipeline that scrapes pgatour.com history into DuckDB, builds features and projects next-year earnings as a distribution for a tiered fantasy golf league.

## Commands

Run with `uv run fantasygolf ...`.

| Command | Status | What it does |
| --- | --- | --- |
| `fantasygolf --version` | available | Print the version. |
| `fantasygolf scrape` | planned | Fetch pgatour.com history into the raw cache. |
| `fantasygolf reconcile` | planned | Reconcile player names across sources. |
| `fantasygolf build-features` | planned | Build model features in DuckDB. |
| `fantasygolf tiers` | planned | Compute the league tiers. |
| `fantasygolf backtest` | planned | Score past projections. |
| `fantasygolf predict` | planned | Project next-year earnings. |
| `fantasygolf trade-check` | planned | Compare a proposed trade. |

## Configuration

Paths default under the working directory: `data/fantasygolf.duckdb`, `data/raw`, `reports`. Override with `FANTASYGOLF_DB`, `FANTASYGOLF_CACHE`, `FANTASYGOLF_REPORTS`.
