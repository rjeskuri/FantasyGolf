"""The source-agnostic DuckDB store: tables, primary keys, upsert and source-scoped ids."""

from pathlib import Path

import duckdb

SCHEMA_VERSION = 2

# Rows per multi-row INSERT in upsert: far faster than one statement per row.
_ROWS_PER_STATEMENT = 500

# table -> (columns as (name, type) in order, primary key columns). No foreign keys: rows may
# arrive before their parents, and no column is specific to one source.
TABLES: dict[str, tuple[list[tuple[str, str]], tuple[str, ...]]] = {
    "players": (
        [
            ("player_id", "TEXT"),
            ("source", "TEXT"),
            ("source_player_id", "TEXT"),
            ("full_name", "TEXT"),
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
            ("country", "TEXT"),
            ("birth_date", "DATE"),
            ("turned_pro", "INTEGER"),
            ("amateur", "BOOLEAN"),
            ("career_earnings", "DOUBLE"),
        ],
        ("player_id",),
    ),
    "player_exemptions": (
        [
            ("source", "TEXT"),
            ("player_id", "TEXT"),
            ("tour", "TEXT"),
            ("description", "TEXT"),
            ("expiration_date", "DATE"),
            ("through_season", "INTEGER"),
        ],
        ("source", "player_id", "tour", "description"),
    ),
    "player_xref": (
        [("player_id", "TEXT"), ("source", "TEXT"), ("source_player_id", "TEXT")],
        ("source", "source_player_id"),
    ),
    "events": (
        [
            ("event_id", "TEXT"),
            ("source", "TEXT"),
            ("source_event_id", "TEXT"),
            ("tour", "TEXT"),
            ("season_label", "INTEGER"),
            ("name", "TEXT"),
            ("start_date", "DATE"),
            ("end_date", "DATE"),
            ("course", "TEXT"),
            ("purse_usd", "DOUBLE"),
            ("official", "BOOLEAN"),
            ("event_kind", "TEXT"),
            ("is_major", "BOOLEAN"),
            ("is_signature", "BOOLEAN"),
            ("is_playoff", "BOOLEAN"),
            ("field_size", "INTEGER"),
            ("field_strength", "DOUBLE"),
        ],
        ("event_id",),
    ),
    "results": (
        [
            ("event_id", "TEXT"),
            ("player_id", "TEXT"),
            ("position_text", "TEXT"),
            ("position", "INTEGER"),
            ("status", "TEXT"),
            ("made_cut", "BOOLEAN"),
            ("rounds_played", "INTEGER"),
            ("total_to_par", "INTEGER"),
            ("earnings_official", "DOUBLE"),
            ("earnings_unofficial", "DOUBLE"),
            ("fedex_points", "DOUBLE"),
        ],
        ("event_id", "player_id"),
    ),
    "round_scores": (
        [
            ("event_id", "TEXT"),
            ("player_id", "TEXT"),
            ("round", "INTEGER"),
            ("strokes", "INTEGER"),
            ("to_par", "INTEGER"),
        ],
        ("event_id", "player_id", "round"),
    ),
    "earnings_other": (
        [
            ("source", "TEXT"),
            ("player_id", "TEXT"),
            ("season_label", "INTEGER"),
            ("component", "TEXT"),
            ("amount_usd", "DOUBLE"),
            ("paid_date", "DATE"),
        ],
        ("source", "player_id", "season_label", "component"),
    ),
    "stat_catalog": (
        [
            ("source", "TEXT"),
            ("season_label", "INTEGER"),
            ("stat_id", "TEXT"),
            ("name", "TEXT"),
            ("category", "TEXT"),
            ("unit", "TEXT"),
            ("higher_is_better", "BOOLEAN"),
        ],
        ("source", "season_label", "stat_id"),
    ),
    "season_stats": (
        [
            ("source", "TEXT"),
            ("player_id", "TEXT"),
            ("season_label", "INTEGER"),
            ("stat_id", "TEXT"),
            ("measure", "TEXT"),
            ("value", "DOUBLE"),
            ("value_text", "TEXT"),
            ("rank", "INTEGER"),
            ("rounds", "INTEGER"),
            ("events", "INTEGER"),
        ],
        ("source", "player_id", "season_label", "stat_id", "measure"),
    ),
    "rankings": (
        [
            ("source", "TEXT"),
            ("ranking", "TEXT"),
            ("as_of_date", "DATE"),
            ("player_id", "TEXT"),
            ("rank", "INTEGER"),
            ("points", "DOUBLE"),
        ],
        ("source", "ranking", "as_of_date", "player_id"),
    ),
}


def connect(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    """Open (or create) the database at path and make sure the schema exists."""
    con = duckdb.connect(str(path))
    init_schema(con)
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create any missing table and record SCHEMA_VERSION once; safe to run again."""
    for table, (columns, key) in TABLES.items():
        body = ", ".join(f'"{name}" {kind}' for name, kind in columns)
        primary = ", ".join(f'"{name}"' for name in key)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({body}, PRIMARY KEY ({primary}))')
    con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    con.execute(
        "INSERT INTO schema_version SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
        [SCHEMA_VERSION],
    )


def upsert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> int:
    """Insert rows, updating any row with the same primary key; return the number of rows passed.

    On an existing key only the columns a row names are written: a column it leaves out keeps
    the stored value, and a column given as None is set to NULL. A new row stores NULL for the
    columns it leaves out. Rows sharing a key in one batch merge in order before writing.
    Table and column names are checked against TABLES before anything is written.
    """
    if table not in TABLES:
        raise ValueError(f"unknown table: {table!r}")
    columns, key = TABLES[table]
    names = [name for name, _ in columns]
    known = set(names)
    for row in rows:
        unknown = sorted(set(row) - known)
        if unknown:
            raise ValueError(f"unknown column(s) for {table}: {', '.join(unknown)}")
    # One merged row per key: a multi-row ON CONFLICT DO UPDATE cannot touch a key twice.
    merged: dict[tuple, dict] = {}
    for row in rows:
        row_key = tuple(row.get(name) for name in key)
        merged[row_key] = merged.get(row_key, {}) | row
    # Rows naming the same columns share one statement.
    groups: dict[tuple[str, ...], list[dict]] = {}
    for row in merged.values():
        named = tuple(name for name in names if name in key or name in row)
        groups.setdefault(named, []).append(row)
    conflict = ", ".join(f'"{name}"' for name in key)
    for named, group in groups.items():
        updates = ", ".join(f'"{name}" = excluded."{name}"' for name in named if name not in key)
        action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
        column_list = ", ".join(f'"{name}"' for name in named)
        marks = "(" + ", ".join("?" for _ in named) + ")"
        for start in range(0, len(group), _ROWS_PER_STATEMENT):
            chunk = group[start : start + _ROWS_PER_STATEMENT]
            con.execute(
                f'INSERT INTO "{table}" ({column_list}) VALUES {", ".join([marks] * len(chunk))}'
                f" ON CONFLICT ({conflict}) {action}",
                [row.get(name) for row in chunk for name in named],
            )
    return len(rows)


def make_id(source: str, source_id: str | int) -> str:
    """The store-wide id for a source's own id: make_id('pgatour', 46046) == 'pgatour:46046'."""
    return f"{source}:{source_id}"
