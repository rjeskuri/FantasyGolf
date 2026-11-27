"""The source-agnostic DuckDB store: schema, primary keys, upsert, make_id, persistence."""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from fantasygolf import store

SCHEMA_VERSION = store.SCHEMA_VERSION
connect = store.connect
init_schema = store.init_schema
make_id = store.make_id
upsert = store.upsert

# The contract from the ticket: table -> (columns in order, declared types, primary key columns).
# Columns without a type in the ticket are not pinned to a type here.
SCHEMA = {
    "players": (
        [
            "player_id",
            "source",
            "source_player_id",
            "full_name",
            "first_name",
            "last_name",
            "country",
            "birth_date",
            "turned_pro",
            "amateur",
        ],
        {"birth_date": "DATE", "turned_pro": "INTEGER", "amateur": "BOOLEAN"},
        {"player_id"},
    ),
    "player_xref": (
        ["player_id", "source", "source_player_id"],
        {},
        {"source", "source_player_id"},
    ),
    "events": (
        [
            "event_id",
            "source",
            "source_event_id",
            "tour",
            "season_label",
            "name",
            "start_date",
            "end_date",
            "course",
            "purse_usd",
            "official",
            "event_kind",
            "is_major",
            "is_signature",
            "is_playoff",
            "field_size",
            "field_strength",
        ],
        {
            "season_label": "INTEGER",
            "start_date": "DATE",
            "end_date": "DATE",
            "purse_usd": "DOUBLE",
            "official": "BOOLEAN",
            "event_kind": "VARCHAR",
            "is_major": "BOOLEAN",
            "is_signature": "BOOLEAN",
            "is_playoff": "BOOLEAN",
            "field_size": "INTEGER",
            "field_strength": "DOUBLE",
        },
        {"event_id"},
    ),
    "results": (
        [
            "event_id",
            "player_id",
            "position_text",
            "position",
            "status",
            "made_cut",
            "rounds_played",
            "total_to_par",
            "earnings_official",
            "earnings_unofficial",
            "fedex_points",
        ],
        {
            "position": "INTEGER",
            "status": "VARCHAR",
            "made_cut": "BOOLEAN",
            "rounds_played": "INTEGER",
            "total_to_par": "INTEGER",
            "earnings_official": "DOUBLE",
            "earnings_unofficial": "DOUBLE",
            "fedex_points": "DOUBLE",
        },
        {"event_id", "player_id"},
    ),
    "earnings_other": (
        ["source", "player_id", "season_label", "component", "amount_usd", "paid_date"],
        {
            "season_label": "INTEGER",
            "component": "VARCHAR",
            "amount_usd": "DOUBLE",
            "paid_date": "DATE",
        },
        {"source", "player_id", "season_label", "component"},
    ),
    "stat_catalog": (
        ["source", "stat_id", "name", "category", "unit", "higher_is_better"],
        {"higher_is_better": "BOOLEAN"},
        {"source", "stat_id"},
    ),
    "season_stats": (
        [
            "source",
            "player_id",
            "season_label",
            "stat_id",
            "value",
            "value_text",
            "rank",
            "rounds",
            "events",
        ],
        {
            "season_label": "INTEGER",
            "value": "DOUBLE",
            "rank": "INTEGER",
            "rounds": "INTEGER",
            "events": "INTEGER",
        },
        {"source", "player_id", "season_label", "stat_id"},
    ),
    "rankings": (
        ["source", "ranking", "as_of_date", "player_id", "rank", "points"],
        {"ranking": "VARCHAR", "as_of_date": "DATE", "rank": "INTEGER", "points": "DOUBLE"},
        {"source", "ranking", "as_of_date", "player_id"},
    ),
}

TABLES = sorted(SCHEMA)

# One full row per table, the value to change in a non-key column, and an alternative value
# for every primary-key column.
SAMPLE = {
    "players": (
        {
            "player_id": "pgatour:46046",
            "source": "pgatour",
            "source_player_id": "46046",
            "full_name": "Scottie Scheffler",
            "first_name": "Scottie",
            "last_name": "Scheffler",
            "country": "USA",
            "birth_date": date(1996, 6, 21),
            "turned_pro": 2018,
            "amateur": False,
        },
        ("country", "United States"),
        {"player_id": "pgatour:28237"},
    ),
    "player_xref": (
        {"player_id": "pgatour:46046", "source": "pgatour", "source_player_id": "46046"},
        ("player_id", "golf:scheffler"),
        {"source": "owgr", "source_player_id": "46047"},
    ),
    "events": (
        {
            "event_id": "pgatour:R2024014",
            "source": "pgatour",
            "source_event_id": "R2024014",
            "tour": "R",
            "season_label": 2024,
            "name": "Masters Tournament",
            "start_date": date(2024, 4, 11),
            "end_date": date(2024, 4, 14),
            "course": "Augusta National Golf Club",
            "purse_usd": 20000000.0,
            "official": True,
            "event_kind": "stroke",
            "is_major": True,
            "is_signature": False,
            "is_playoff": False,
            "field_size": 89,
            "field_strength": 412.5,
        },
        ("purse_usd", 18000000.0),
        {"event_id": "pgatour:R2024018"},
    ),
    "results": (
        {
            "event_id": "pgatour:R2024014",
            "player_id": "pgatour:46046",
            "position_text": "1",
            "position": 1,
            "status": "finished",
            "made_cut": True,
            "rounds_played": 4,
            "total_to_par": -11,
            "earnings_official": 3600000.0,
            "earnings_unofficial": 0.0,
            "fedex_points": 750.0,
        },
        ("position", 2),
        {"event_id": "pgatour:R2024018", "player_id": "pgatour:28237"},
    ),
    "earnings_other": (
        {
            "source": "pgatour",
            "player_id": "pgatour:46046",
            "season_label": 2024,
            "component": "fedex_bonus",
            "amount_usd": 25000000.0,
            "paid_date": date(2024, 8, 25),
        },
        ("amount_usd", 18000000.0),
        {
            "source": "espn",
            "player_id": "pgatour:28237",
            "season_label": 2023,
            "component": "comcast_top10",
        },
    ),
    "stat_catalog": (
        {
            "source": "pgatour",
            "stat_id": "02675",
            "name": "SG: Total",
            "category": "strokes_gained",
            "unit": "strokes",
            "higher_is_better": True,
        },
        ("name", "Strokes Gained: Total"),
        {"source": "datagolf", "stat_id": "02674"},
    ),
    "season_stats": (
        {
            "source": "pgatour",
            "player_id": "pgatour:46046",
            "season_label": 2024,
            "stat_id": "02675",
            "value": 2.95,
            "value_text": "2.950",
            "rank": 1,
            "rounds": 80,
            "events": 19,
        },
        ("value", 3.1),
        {
            "source": "datagolf",
            "player_id": "pgatour:28237",
            "season_label": 2019,
            "stat_id": "02674",
        },
    ),
    "rankings": (
        {
            "source": "owgr",
            "ranking": "owgr",
            "as_of_date": date(2024, 12, 29),
            "player_id": "pgatour:46046",
            "rank": 1,
            "points": 19.1,
        },
        ("rank", 3),
        {
            "ranking": "fedex_cup",
            "source": "pgatour",
            "as_of_date": date(2024, 1, 7),
            "player_id": "pgatour:28237",
        },
    ),
}


def rows(con, table, columns):
    order = ", ".join(columns)
    return con.execute(f"SELECT {order} FROM {table} ORDER BY {order}").fetchall()


def count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def base_tables(con):
    found = con.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {name for (name,) in found}


def columns_of(con, table):
    return con.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()


def primary_key_of(con, table):
    found = con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints()"
        " WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
        [table],
    ).fetchall()
    return [set(cols) for (cols,) in found]


# --- the tables -----------------------------------------------------------------------------


def test_connect_creates_all_tables():
    con = connect(":memory:")
    assert isinstance(con, duckdb.DuckDBPyConnection)
    assert set(SCHEMA) | {"schema_version"} <= base_tables(con)


def test_connect_defaults_to_in_memory():
    con = connect()
    assert set(SCHEMA) | {"schema_version"} <= base_tables(con)


@pytest.mark.parametrize("table", TABLES)
def test_table_has_exactly_the_contract_columns_and_types(table):
    columns, types, _ = SCHEMA[table]
    found = columns_of(connect(), table)
    assert [name for name, _ in found] == columns
    found_types = dict(found)
    for column, declared in types.items():
        assert found_types[column] == declared, column


@pytest.mark.parametrize("table", TABLES)
def test_table_primary_key_is_declared(table):
    _, _, key = SCHEMA[table]
    assert primary_key_of(connect(), table) == [key]


def test_no_source_specific_columns():
    con = connect()
    for table in TABLES:
        for name, _ in columns_of(con, table):
            assert "pgatour" not in name.lower(), (table, name)


def test_schema_version_records_the_module_version():
    recorded = connect().execute("SELECT * FROM schema_version").fetchall()
    assert len(recorded) == 1
    assert SCHEMA_VERSION in recorded[0]


def test_init_schema_twice_is_safe():
    con = connect()
    for table in TABLES:
        upsert(con, table, [SAMPLE[table][0]])
    versions_before = con.execute("SELECT * FROM schema_version").fetchall()

    assert init_schema(con) is None
    init_schema(con)

    assert set(SCHEMA) | {"schema_version"} <= base_tables(con)
    assert con.execute("SELECT * FROM schema_version").fetchall() == versions_before
    for table in TABLES:
        columns = SCHEMA[table][0]
        expected = [tuple(SAMPLE[table][0][c] for c in columns)]
        assert rows(con, table, columns) == expected, table


def test_init_schema_on_a_bare_connection_creates_the_tables():
    con = duckdb.connect(":memory:")
    init_schema(con)
    assert set(SCHEMA) | {"schema_version"} <= base_tables(con)


# --- upsert -----------------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_upsert_is_idempotent_by_primary_key(table):
    columns = SCHEMA[table][0]
    row, (column, new_value), _ = SAMPLE[table]
    con = connect()

    assert upsert(con, table, [row]) == 1
    assert upsert(con, table, [row]) == 1
    assert count(con, table) == 1

    updated = {**row, column: new_value}
    assert upsert(con, table, [updated]) == 1
    assert rows(con, table, columns) == [tuple(updated[c] for c in columns)]


@pytest.mark.parametrize(
    ("table", "key_column"),
    [(table, column) for table in TABLES for column in sorted(SCHEMA[table][2])],
)
def test_upsert_keeps_rows_that_differ_in_one_key_column(table, key_column):
    columns = SCHEMA[table][0]
    row, _, alternatives = SAMPLE[table]
    other = {**row, key_column: alternatives[key_column]}
    con = connect()

    assert upsert(con, table, [row, other]) == 2
    assert count(con, table) == 2
    expected = sorted(tuple(r[c] for c in columns) for r in (row, other))
    assert rows(con, table, columns) == expected


@pytest.mark.parametrize("n", [1, 3, 7])
def test_upsert_returns_rows_written(n):
    con = connect()
    batch = [
        {
            "source": "pgatour",
            "stat_id": f"{i:05d}",
            "name": f"stat {i}",
            "higher_is_better": i % 2 == 0,
        }
        for i in range(n)
    ]
    assert upsert(con, "stat_catalog", batch) == n
    assert count(con, "stat_catalog") == n
    assert rows(con, "stat_catalog", ["stat_id", "name", "higher_is_better"]) == [
        (f"{i:05d}", f"stat {i}", i % 2 == 0) for i in range(n)
    ]


def test_upsert_empty_rows_returns_zero():
    con = connect()
    assert upsert(con, "players", []) == 0
    assert count(con, "players") == 0


def test_upsert_partial_row_leaves_other_columns_null():
    con = connect()
    assert upsert(con, "players", [{"player_id": "espn:9478", "full_name": "Rory McIlroy"}]) == 1
    columns = SCHEMA["players"][0]
    (got,) = rows(con, "players", columns)
    expected = {c: None for c in columns} | {"player_id": "espn:9478", "full_name": "Rory McIlroy"}
    assert got == tuple(expected[c] for c in columns)


@pytest.mark.parametrize(
    ("table", "bad_column"),
    [("players", "nickname"), ("results", "earnings_total"), ("events", "pgatour_id")],
)
def test_upsert_rejects_unknown_column(table, bad_column):
    con = connect()
    row = {**SAMPLE[table][0], bad_column: "x"}
    with pytest.raises(ValueError, match=bad_column):
        upsert(con, table, [row])
    assert count(con, table) == 0


def test_upsert_rejects_unknown_column_in_a_later_row():
    con = connect()
    good = SAMPLE["players"][0]
    bad = {"player_id": "espn:1", "handedness": "left"}
    with pytest.raises(ValueError, match="handedness"):
        upsert(con, "players", [good, bad])


@pytest.mark.parametrize("table", ["nosuch", "schema_version_x", "players; DROP TABLE events"])
def test_upsert_rejects_unknown_table(table):
    con = connect()
    with pytest.raises(ValueError):
        upsert(con, table, [{"player_id": "pgatour:1"}])
    assert set(SCHEMA) <= base_tables(con)


# --- earnings components ------------------------------------------------------------------------


def component_sums(con):
    official, unofficial = con.execute(
        "SELECT sum(earnings_official), sum(earnings_unofficial) FROM results"
    ).fetchone()
    other = dict(
        con.execute(
            "SELECT component, sum(amount_usd) FROM earnings_other GROUP BY component"
        ).fetchall()
    )
    return official, unofficial, other


def test_earnings_components_stored_separately():
    con = connect()
    upsert(
        con,
        "results",
        [
            {
                "event_id": "pgatour:R2024014",
                "player_id": "pgatour:46046",
                "earnings_official": 1000.0,
                "earnings_unofficial": 50.0,
            }
        ],
    )
    upsert(
        con,
        "earnings_other",
        [
            {
                "source": "pgatour",
                "player_id": "pgatour:46046",
                "season_label": 2024,
                "component": "fedex_bonus",
                "amount_usd": 200.0,
            }
        ],
    )
    assert component_sums(con) == (1000.0, 50.0, {"fedex_bonus": 200.0})


def test_earnings_components_sum_independently_over_many_rows():
    results = [
        ("pgatour:E1", "pgatour:P1", 1000.0, 50.0),
        ("pgatour:E1", "pgatour:P2", 700.0, 0.0),
        ("pgatour:E2", "pgatour:P1", 300.0, 25.0),
        ("pgatour:E3", "pgatour:P3", 0.0, 400.0),
    ]
    other = [
        ("pgatour:P1", 2024, "fedex_bonus", 200.0),
        ("pgatour:P1", 2024, "comcast_top10", 90.0),
        ("pgatour:P2", 2024, "fedex_bonus", 1500.0),
        ("pgatour:P1", 2023, "fedex_bonus", 60.0),
        ("pgatour:P3", 2024, "other", 7.0),
    ]
    con = connect()
    upsert(
        con,
        "results",
        [
            {"event_id": e, "player_id": p, "earnings_official": o, "earnings_unofficial": u}
            for e, p, o, u in results
        ],
    )
    upsert(
        con,
        "earnings_other",
        [
            {
                "source": "pgatour",
                "player_id": p,
                "season_label": s,
                "component": c,
                "amount_usd": a,
            }
            for p, s, c, a in other
        ],
    )

    expected_other = {}
    for _, _, component, amount in other:
        expected_other[component] = expected_other.get(component, 0.0) + amount
    assert component_sums(con) == (2000.0, 475.0, expected_other)
    assert expected_other == {"fedex_bonus": 1760.0, "comcast_top10": 90.0, "other": 7.0}
    assert count(con, "earnings_other") == len(other)


# --- make_id ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "source_id", "expected"),
    [
        ("pgatour", 46046, "pgatour:46046"),
        ("pgatour", "46046", "pgatour:46046"),
        ("pgatour", "R2024014", "pgatour:R2024014"),
        ("owgr", 7, "owgr:7"),
        ("espn", "9478", "espn:9478"),
        ("datagolf", 0, "datagolf:0"),
    ],
)
def test_make_id(source, source_id, expected):
    assert make_id(source, source_id) == expected


def test_make_id_distinguishes_sources_sharing_an_id():
    assert make_id("pgatour", 100) != make_id("espn", 100)


def test_two_sources_coexist_for_the_same_player():
    con = connect()
    upsert(
        con,
        "player_xref",
        [
            {
                "player_id": make_id("pgatour", 46046),
                "source": "pgatour",
                "source_player_id": "46046",
            },
            {"player_id": make_id("pgatour", 46046), "source": "espn", "source_player_id": "9478"},
            {"player_id": make_id("pgatour", 28237), "source": "espn", "source_player_id": "46046"},
        ],
    )
    assert rows(con, "player_xref", ["source", "source_player_id", "player_id"]) == [
        ("espn", "46046", "pgatour:28237"),
        ("espn", "9478", "pgatour:46046"),
        ("pgatour", "46046", "pgatour:46046"),
    ]


# --- persistence --------------------------------------------------------------------------------


@pytest.mark.parametrize("as_str", [False, True])
def test_reopen_file_keeps_rows(tmp_path, as_str):
    path: str | Path = tmp_path / "fantasygolf.duckdb"
    if as_str:
        path = str(path)
    con = connect(path)
    for table in TABLES:
        upsert(con, table, [SAMPLE[table][0]])
    con.close()

    assert Path(path).is_file()
    con = connect(path)
    for table in TABLES:
        columns = SCHEMA[table][0]
        assert rows(con, table, columns) == [tuple(SAMPLE[table][0][c] for c in columns)], table
    assert len(con.execute("SELECT * FROM schema_version").fetchall()) == 1
    con.close()
