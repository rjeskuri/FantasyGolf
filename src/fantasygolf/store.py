"""The source-agnostic DuckDB store: tables, primary keys, upsert and source-scoped ids."""

from datetime import date, datetime
from pathlib import Path

import duckdb

# The shape of the tables below; a file recording another version is refused, not migrated.
SCHEMA_VERSION = 3

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
    # Team events: which players formed each team, and the rounds reported once per team.
    "event_teams": (
        [("event_id", "TEXT"), ("team_id", "TEXT"), ("player_id", "TEXT")],
        ("event_id", "team_id", "player_id"),
    ),
    "team_round_scores": (
        [
            ("event_id", "TEXT"),
            ("team_id", "TEXT"),
            ("round", "INTEGER"),
            ("strokes", "INTEGER"),
            ("to_par", "INTEGER"),
        ],
        ("event_id", "team_id", "round"),
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
    """Create any missing table and record SCHEMA_VERSION once; safe to run again.

    Raises RuntimeError, before writing anything, when the database records another version.
    """
    recorded = _recorded_version(con)
    if recorded is not None and recorded != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {recorded} does not match this store's version"
            f" {SCHEMA_VERSION}; rebuild the database (there is no migration)"
        )
    for table, (columns, key) in TABLES.items():
        body = ", ".join(f'"{name}" {kind}' for name, kind in columns)
        primary = ", ".join(f'"{name}"' for name in key)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({body}, PRIMARY KEY ({primary}))')
    con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    con.execute(
        "INSERT INTO schema_version SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
        [SCHEMA_VERSION],
    )


def _recorded_version(con: duckdb.DuckDBPyConnection) -> int | None:
    """The version in the schema_version table, or None when there is no table or no row."""
    exists = con.execute(
        "SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = current_schema() AND table_name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return None
    return con.execute("SELECT max(version) FROM schema_version").fetchone()[0]


def _as_sql_value(kind: str, value: object) -> object:
    """value as its column's SQL type stores it, so one key compares equal in Python.

    Only the common spellings are normalized (digits for an INTEGER, an ISO string or a
    datetime for a DATE, an int for TEXT); anything else is left for DuckDB to cast or reject.
    """
    if value is None or isinstance(value, bool):
        return value
    try:
        if kind == "TEXT" and isinstance(value, int):
            return str(value)
        if kind == "INTEGER" and isinstance(value, str):
            return int(value.strip())
        if kind == "DATE" and isinstance(value, datetime):
            return value.date()
        if kind == "DATE" and isinstance(value, str):
            return date.fromisoformat(value.strip())
    except ValueError:
        pass
    return value


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
    kinds = dict(columns)
    known = set(names)
    for row in rows:
        unknown = sorted(set(row) - known)
        if unknown:
            raise ValueError(f"unknown column(s) for {table}: {', '.join(unknown)}")
    # One merged row per key: a multi-row ON CONFLICT DO UPDATE cannot touch a key twice. Keys
    # compare as their SQL values, so a date and its ISO string (or 2 and "2") are one key.
    merged: dict[tuple, dict] = {}
    for row in rows:
        row_key = tuple(_as_sql_value(kinds[name], row.get(name)) for name in key)
        merged[row_key] = merged.get(row_key, {}) | row | dict(zip(key, row_key, strict=True))
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
