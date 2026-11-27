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

# The contract from the ticket, widened by the owner's round-1 answer (season_label in
# stat_catalog's key, measure in season_stats' key, round_scores, players.career_earnings and
# player_exemptions): table -> (columns in order, declared types, primary key columns).
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
            "career_earnings",
        ],
        {
            "birth_date": "DATE",
            "turned_pro": "INTEGER",
            "amateur": "BOOLEAN",
            "career_earnings": "DOUBLE",
        },
        {"player_id"},
    ),
    "player_exemptions": (
        ["source", "player_id", "tour", "description", "expiration_date", "through_season"],
        {
            "tour": "VARCHAR",
            "description": "VARCHAR",
            "expiration_date": "DATE",
            "through_season": "INTEGER",
        },
        {"source", "player_id", "tour", "description"},
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
    "round_scores": (
        ["event_id", "player_id", "round", "strokes", "to_par"],
        {"round": "INTEGER", "strokes": "INTEGER", "to_par": "INTEGER"},
        {"event_id", "player_id", "round"},
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
        ["source", "season_label", "stat_id", "name", "category", "unit", "higher_is_better"],
        {"season_label": "INTEGER", "higher_is_better": "BOOLEAN"},
        {"source", "season_label", "stat_id"},
    ),
    "season_stats": (
        [
            "source",
            "player_id",
            "season_label",
            "stat_id",
            "measure",
            "value",
            "value_text",
            "rank",
            "rounds",
            "events",
        ],
        {
            "season_label": "INTEGER",
            "measure": "VARCHAR",
            "value": "DOUBLE",
            "rank": "INTEGER",
            "rounds": "INTEGER",
            "events": "INTEGER",
        },
        {"source", "player_id", "season_label", "stat_id", "measure"},
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
            "career_earnings": 130955999.0,
        },
        ("country", "United States"),
        {"player_id": "pgatour:28237"},
    ),
    "player_exemptions": (
        {
            "source": "pgatour",
            "player_id": "pgatour:46046",
            "tour": "R",
            "description": "Winner, PGA Championship (thru 2031)",
            "expiration_date": date(2031, 12, 31),
            "through_season": 2031,
        },
        ("through_season", 2030),
        {
            "source": "espn",
            "player_id": "pgatour:28237",
            "tour": "S",
            "description": "Winner, Masters Tournament (thru 2029)",
        },
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
    "round_scores": (
        {
            "event_id": "pgatour:R2024014",
            "player_id": "pgatour:46046",
            "round": 1,
            "strokes": 66,
            "to_par": -6,
        },
        ("strokes", 67),
        {"event_id": "pgatour:R2024018", "player_id": "pgatour:28237", "round": 4},
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
            "season_label": 2024,
            "stat_id": "02675",
            "name": "SG: Total",
            "category": "strokes_gained",
            "unit": "strokes",
            "higher_is_better": True,
        },
        ("name", "Strokes Gained: Total"),
        {"source": "datagolf", "season_label": 2019, "stat_id": "02674"},
    ),
    "season_stats": (
        {
            "source": "pgatour",
            "player_id": "pgatour:46046",
            "season_label": 2024,
            "stat_id": "02675",
            "measure": "Avg",
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
            "measure": "Total SG:T",
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
    assert len(SCHEMA) == 10
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
            "season_label": 2024,
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


# A row that leaves a column out keeps the stored value on conflict; a column present with None
# clears it (the owner's round-1 answer).


def stored(con, table):
    columns = SCHEMA[table][0]
    (got,) = rows(con, table, columns)
    return dict(zip(columns, got, strict=True))


@pytest.mark.parametrize("table", TABLES)
def test_upsert_sparse_row_keeps_omitted_columns_and_none_clears(table):
    columns, _, key = SCHEMA[table]
    row, (changed, new_value), _ = SAMPLE[table]
    key_only = {c: row[c] for c in key}
    cleared = next((c for c in columns if c not in key and c != changed), changed)
    assert row[cleared] is not None
    con = connect()
    upsert(con, table, [row])

    assert upsert(con, table, [key_only]) == 1
    assert stored(con, table) == row

    assert upsert(con, table, [key_only | {changed: new_value}]) == 1
    assert stored(con, table) == row | {changed: new_value}

    assert upsert(con, table, [key_only | {cleared: None}]) == 1
    assert stored(con, table) == row | {changed: new_value, cleared: None}
    assert count(con, table) == 1


def test_bio_then_sparse_result_player_upsert_keeps_bio_fields():
    bio = {
        "player_id": "pgatour:46046",
        "source": "pgatour",
        "source_player_id": "46046",
        "full_name": "Scottie Scheffler",
        "first_name": "Scottie",
        "last_name": "Scheffler",
        "country": "United States",
        "birth_date": date(1996, 6, 21),
        "turned_pro": 2018,
        "career_earnings": 130955999.0,
    }
    from_result = {
        "player_id": "pgatour:46046",
        "source": "pgatour",
        "source_player_id": "46046",
        "full_name": "Scottie Scheffler",
        "country": "USA",
        "amateur": False,
    }
    con = connect()
    upsert(con, "players", [bio])
    upsert(con, "players", [from_result])

    expected = {c: None for c in SCHEMA["players"][0]} | bio | from_result
    assert stored(con, "players") == expected
    assert expected["birth_date"] == date(1996, 6, 21)
    assert expected["turned_pro"] == 2018

    upsert(con, "players", [{"player_id": "pgatour:46046", "turned_pro": None}])
    assert stored(con, "players") == expected | {"turned_pro": None}


def test_upsert_merges_rows_sharing_a_key_within_one_batch():
    con = connect()
    batch = [
        {"player_id": "pgatour:46046", "country": "United States", "birth_date": date(1996, 6, 21)},
        {"player_id": "pgatour:28237", "full_name": "Rory McIlroy", "turned_pro": 2007},
        {"player_id": "pgatour:46046", "country": "USA", "amateur": False},
        {"player_id": "pgatour:28237", "turned_pro": None, "country": "NIR"},
    ]
    assert upsert(con, "players", batch) == 4
    got = {
        player: (country, birth_date, amateur, full_name, turned_pro)
        for player, country, birth_date, amateur, full_name, turned_pro in rows(
            con,
            "players",
            ["player_id", "country", "birth_date", "amateur", "full_name", "turned_pro"],
        )
    }
    assert got == {
        "pgatour:46046": ("USA", date(1996, 6, 21), False, None, None),
        "pgatour:28237": ("NIR", None, None, "Rory McIlroy", None),
    }


def test_upsert_sparse_batch_updates_each_row_by_its_own_columns():
    con = connect()
    full = [
        {**SAMPLE["results"][0], "event_id": f"pgatour:R20240{n:02d}", "position": n}
        for n in range(1, 5)
    ]
    upsert(con, "results", full)
    sparse = [
        {"event_id": "pgatour:R2024001", "player_id": "pgatour:46046", "fedex_points": 10.0},
        {"event_id": "pgatour:R2024002", "player_id": "pgatour:46046", "position": None},
        {"event_id": "pgatour:R2024003", "player_id": "pgatour:46046", "status": "wd"},
    ]
    assert upsert(con, "results", sparse) == 3
    got = rows(con, "results", ["event_id", "position", "status", "fedex_points", "total_to_par"])
    assert got == [
        ("pgatour:R2024001", 1, "finished", 10.0, -11),
        ("pgatour:R2024002", None, "finished", 750.0, -11),
        ("pgatour:R2024003", 3, "wd", 750.0, -11),
        ("pgatour:R2024004", 4, "finished", 750.0, -11),
    ]


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


# --- the widened schema (owner's round-1 answer) -----------------------------------------------


def test_stat_catalog_keeps_each_seasons_classification_across_reopen(tmp_path):
    path = tmp_path / "catalog.duckdb"
    entries = [
        ("pgatour", 2012, "02675", "SG: Total", "STROKES_GAINED"),
        ("pgatour", 2019, "02675", "SG: Total", "SCORING"),
        ("pgatour", 2024, "02675", "SG: Total", "STROKES_GAINED"),
        ("pgatour", 2019, "109", "Official Money", "MONEY_FINISHES"),
        ("pgatour", 2024, "109", "Official Money", "MONEY"),
        ("datagolf", 2024, "02675", "SG Total", "SG"),
    ]
    con = connect(path)
    for source, season, stat_id, name, category in entries:
        upsert(
            con,
            "stat_catalog",
            [
                {
                    "source": source,
                    "season_label": season,
                    "stat_id": stat_id,
                    "name": name,
                    "category": category,
                }
            ],
        )
    con.close()

    con = connect(path)
    got = rows(con, "stat_catalog", ["source", "season_label", "stat_id", "name", "category"])
    assert got == sorted(entries)
    by_season = dict(
        con.execute(
            "SELECT season_label, category FROM stat_catalog"
            " WHERE source = 'pgatour' AND stat_id = '02675'"
        ).fetchall()
    )
    assert by_season == {2012: "STROKES_GAINED", 2019: "SCORING", 2024: "STROKES_GAINED"}
    con.close()


def test_season_stats_holds_every_measure_of_one_stat():
    # Stat 109's statHeaders are ["Money", "YTD Victories"]: one row per measure.
    money = [
        ("pgatour:46046", 2024, 29228357.0, "$29,228,357", 7.0, "7", 1),
        ("pgatour:47959", 2024, 20564233.0, "$20,564,233", 1.0, "1", 2),
        ("pgatour:34046", 2019, 9684006.0, "$9,684,006", 3.0, "3", 1),
    ]
    stat_rows = []
    for player, season, dollars, dollars_text, wins, wins_text, rank in money:
        base = {
            "source": "pgatour",
            "player_id": player,
            "season_label": season,
            "stat_id": "109",
            "rank": rank,
        }
        stat_rows.append(base | {"measure": "Money", "value": dollars, "value_text": dollars_text})
        stat_rows.append(
            base | {"measure": "YTD Victories", "value": wins, "value_text": wins_text}
        )
    con = connect()
    assert upsert(con, "season_stats", stat_rows) == 2 * len(money)
    assert count(con, "season_stats") == 2 * len(money)

    for player, season, dollars, dollars_text, wins, wins_text, rank in money:
        got = con.execute(
            "SELECT measure, value, value_text, rank FROM season_stats"
            " WHERE source = 'pgatour' AND player_id = ? AND season_label = ? AND stat_id = '109'"
            " ORDER BY measure",
            [player, season],
        ).fetchall()
        assert got == [
            ("Money", dollars, dollars_text, rank),
            ("YTD Victories", wins, wins_text, rank),
        ], player

    # Rewriting one measure leaves the other alone.
    upsert(
        con,
        "season_stats",
        [
            {
                "source": "pgatour",
                "player_id": "pgatour:46046",
                "season_label": 2024,
                "stat_id": "109",
                "measure": "YTD Victories",
                "value": 8.0,
            }
        ],
    )
    got = con.execute(
        "SELECT measure, value FROM season_stats"
        " WHERE player_id = 'pgatour:46046' AND stat_id = '109' ORDER BY measure"
    ).fetchall()
    assert got == [("Money", 29228357.0), ("YTD Victories", 8.0)]


def test_round_scores_survive_reopening_a_file(tmp_path):
    # From the 2024 Masters fixture: Scheffler 66 72 71 68 (-6 E -1 -4); a cut player 74 77.
    scores = {
        ("pgatour:R2024014", "pgatour:46046"): [(66, -6), (72, 0), (71, -1), (68, -4)],
        ("pgatour:R2024014", "pgatour:30911"): [(74, 2), (77, 5)],
        ("pgatour:R2019464", "pgatour:46046"): [(70, -2), (65, -7), (69, -3)],
    }
    score_rows = [
        {"event_id": e, "player_id": p, "round": n, "strokes": s, "to_par": t}
        for (e, p), rounds in scores.items()
        for n, (s, t) in enumerate(rounds, start=1)
    ]
    path = tmp_path / "rounds.duckdb"
    con = connect(path)
    assert upsert(con, "round_scores", score_rows) == 9
    con.close()

    con = connect(path)
    for (event, player), rounds in scores.items():
        got = con.execute(
            "SELECT round, strokes, to_par FROM round_scores"
            " WHERE event_id = ? AND player_id = ? ORDER BY round",
            [event, player],
        ).fetchall()
        assert got == [(n, s, t) for n, (s, t) in enumerate(rounds, start=1)]
    totals = con.execute(
        "SELECT sum(strokes), sum(to_par) FROM round_scores"
        " WHERE event_id = 'pgatour:R2024014' AND player_id = 'pgatour:46046'"
    ).fetchone()
    assert totals == (277, -11)
    con.close()


def test_career_earnings_and_exemptions_round_trip(tmp_path):
    players = [
        {"player_id": "pgatour:46046", "full_name": "S. Scheffler", "career_earnings": 130955999.0},
        {"player_id": "pgatour:28237", "full_name": "R. McIlroy", "career_earnings": 98000000.5},
        {"player_id": "pgatour:01810", "full_name": "Phil Mickelson"},
    ]
    exemptions = [
        ("pgatour", "pgatour:46046", "R", "Winner, PGA Championship (thru 2031)", None, 2031),
        ("pgatour", "pgatour:46046", "R", "Winner, Masters Tournament (thru 2029)", None, 2029),
        ("pgatour", "pgatour:46046", "S", "Winner, PGA Championship (thru 2031)", None, 2031),
        ("pgatour", "pgatour:28237", "R", "Career money top 50", date(2026, 12, 31), None),
        ("dpworld", "pgatour:28237", "E", "Race to Dubai winner", date(2027, 11, 30), 2027),
    ]
    columns = SCHEMA["player_exemptions"][0]
    path = tmp_path / "bio.duckdb"
    con = connect(path)
    upsert(con, "players", players)
    exemption_rows = [dict(zip(columns, e, strict=True)) for e in exemptions]
    assert upsert(con, "player_exemptions", exemption_rows) == len(exemptions)
    con.close()

    con = connect(path)
    assert rows(con, "players", ["player_id", "career_earnings"]) == sorted(
        (p["player_id"], p.get("career_earnings")) for p in players
    )
    got = con.execute(
        f"SELECT {', '.join(columns)} FROM player_exemptions"
        " ORDER BY source, player_id, tour, description"
    ).fetchall()
    assert got == sorted(exemptions, key=lambda e: e[:4])
    per_player = dict(
        con.execute(
            "SELECT player_id, count(*) FROM player_exemptions GROUP BY player_id"
        ).fetchall()
    )
    assert per_player == {"pgatour:46046": 3, "pgatour:28237": 2}
    con.close()
