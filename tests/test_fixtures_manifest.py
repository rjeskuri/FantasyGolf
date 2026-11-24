"""The recorded pgatour.com fixtures, their manifest, the endpoint registry and FixtureClient."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

from fakes import FixtureClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pgatour"
MANIFEST = FIXTURE_DIR / "manifest.json"

REQUIRED_FAMILIES = {
    "schedule",
    "event_results",
    "stat_catalog",
    "season_stat",
    "money_list",
    "player_list",
    "player_bio",
}
ENTRY_KEYS = {"family", "file", "request", "season", "expect"}
SECRET_MARKERS = ("x-api-key", "cookie", "authorization")
# AppSync API keys look like "da2-" followed by 26 lowercase alphanumerics.
APPSYNC_KEY = re.compile(r"da2-[a-z0-9]{20,}")
# Player fields that hold personal detail (family, hometown, residence, life narrative) that a
# fantasy earnings model does not need: never requested, never recorded.
PERSONAL_FIELDS = ("family", "personal", "birthplace", "residence", "playsFrom")
PERSONAL_FIELD_TOKEN = re.compile(r"\b(" + "|".join(PERSONAL_FIELDS) + r")\b")
RELATIVE_WORDS = re.compile(
    r"\b(wife|husband|spouse|father|mother|sister|brother|son|daughter|children|married)\b",
    re.IGNORECASE,
)


def _entries() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]


def _payload(entry: dict) -> dict | None:
    request = entry["request"]
    return request.get("params") if request["method"] == "GET" else request.get("body")


def _call(client: FixtureClient, method: str, url: str, payload: dict | None) -> dict:
    if method == "GET":
        return client.get_json(url, payload)
    return client.post_json(url, payload)


def _reorder(value):
    """The same JSON value with every dict's keys in reverse order."""
    if isinstance(value, dict):
        return {k: _reorder(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reorder(v) for v in value]
    return value


def test_every_manifest_entry_points_to_valid_json():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest) == {"fixtures"}
    entries = manifest["fixtures"]
    assert isinstance(entries, list) and entries

    listed = []
    for entry in entries:
        name = entry.get("file")
        assert set(entry) == ENTRY_KEYS, name
        assert isinstance(entry["family"], str) and entry["family"], name
        assert entry["season"] is None or isinstance(entry["season"], int), name
        assert isinstance(entry["expect"], dict) and entry["expect"], name

        request = entry["request"]
        assert "headers" not in request, name
        assert request["method"] in {"GET", "POST"}, name
        assert request["url"].startswith("https://"), name
        if request["method"] == "GET":
            assert set(request) <= {"method", "url", "params"}, name
            assert isinstance(request.get("params", {}), dict), name
        else:
            assert set(request) == {"method", "url", "body"}, name
            body = request["body"]
            assert isinstance(body, dict), name
            if "query" in body:  # GraphQL: a named operation with its variables
                assert isinstance(body["query"], str) and body["query"].strip(), name
                assert isinstance(body.get("operationName"), str), name
                assert isinstance(body.get("variables", {}), dict), name

        path = (FIXTURE_DIR / name).resolve()
        assert path.is_relative_to(FIXTURE_DIR.resolve()), name
        assert path.is_file(), name
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), name
        assert not data.get("errors"), f"{name} recorded a GraphQL error response"
        listed.append(path)

    assert len(listed) == len(set(listed)), "a fixture file is listed twice"
    on_disk = {p.resolve() for p in FIXTURE_DIR.rglob("*.json") if p.resolve() != MANIFEST}
    assert on_disk == set(listed), "every fixture file has exactly one manifest entry"


def test_required_families_present():
    counts: dict[str, int] = {}
    for entry in _entries():
        counts[entry["family"]] = counts.get(entry["family"], 0) + 1
    missing = REQUIRED_FAMILIES - counts.keys()
    assert not missing, f"no fixture for {sorted(missing)}"
    for family in REQUIRED_FAMILIES:
        assert counts[family] >= 1


def test_fixtures_cover_required_seasons_and_team_event():
    entries = _entries()
    seasons = {e["season"] for e in entries if e["season"] is not None}
    assert any(1990 <= s <= 1999 for s in seasons), "a 1990s season"
    for season in (2019, 2023, 2024):
        assert season in seasons, season
    schedule_seasons = {e["season"] for e in entries if e["family"] == "schedule"}
    assert {2019, 2024} <= schedule_seasons, "a wraparound and a calendar schedule"
    results_seasons = {e["season"] for e in entries if e["family"] == "event_results"}
    assert {2019, 2023, 2024} <= results_seasons
    assert any(1990 <= s <= 1999 for s in results_seasons if s is not None)
    team_events = [
        e
        for e in entries
        if e["family"] == "event_results" and e["expect"].get("team_row_count", 0) > 0
    ]
    assert team_events, "a team event (Zurich Classic)"


def test_every_family_has_an_endpoint():
    from fantasygolf.sources.pgatour import endpoints

    assert isinstance(endpoints.OFFICIAL_MONEY_STAT_ID, str) and endpoints.OFFICIAL_MONEY_STAT_ID
    assert issubclass(endpoints.ParseError, ValueError)
    assert dataclasses.is_dataclass(endpoints.Endpoint)
    assert endpoints.Endpoint.__dataclass_params__.frozen
    assert {f.name for f in dataclasses.fields(endpoints.Endpoint)} == {
        "family",
        "method",
        "url",
        "query",
        "notes",
    }

    registry = endpoints.ENDPOINTS
    assert REQUIRED_FAMILIES <= registry.keys()
    for key, endpoint in registry.items():
        assert isinstance(endpoint, endpoints.Endpoint), key
        assert endpoint.family == key
        assert endpoint.method in {"GET", "POST"}, key
        assert endpoint.url.startswith("https://"), key
        assert endpoint.query is None or (isinstance(endpoint.query, str) and endpoint.query)
        assert isinstance(endpoint.notes, str), key

    for entry in _entries():
        family = entry["family"]
        assert family in registry, family
        endpoint = registry[family]
        request = entry["request"]
        assert endpoint.method == request["method"], entry["file"]
        assert endpoint.url == request["url"], entry["file"]
        body = request.get("body") or {}
        if "query" in body:
            # The fixture was recorded with the registry's GraphQL document, so an ingest that
            # sends ENDPOINTS[family].query reproduces the recorded request.
            assert endpoint.query == body["query"], entry["file"]

    money_ids = {
        e["request"]["body"]["variables"]["statId"]
        for e in _entries()
        if e["family"] == "money_list"
    }
    assert money_ids == {endpoints.OFFICIAL_MONEY_STAT_ID}


def test_fixture_client_serves_manifest_requests():
    entries = _entries()
    client = FixtureClient(MANIFEST)
    assert client.calls == 0
    for n, entry in enumerate(entries, start=1):
        request = entry["request"]
        expected = json.loads((FIXTURE_DIR / entry["file"]).read_text(encoding="utf-8"))
        got = _call(client, request["method"], request["url"], _payload(entry))
        assert got == expected, entry["file"]
        assert client.calls == n

    # Payloads compare by value: the same request with its keys reordered still matches.
    first = entries[0]
    reordered = _reorder(_payload(first))
    assert list(reordered) != list(_payload(first))
    expected = json.loads((FIXTURE_DIR / first["file"]).read_text(encoding="utf-8"))
    got = _call(client, first["request"]["method"], first["request"]["url"], reordered)
    assert got == expected
    assert client.calls == len(entries) + 1

    # A caller mutating the result cannot change what the next call returns.
    got.clear()
    again = _call(client, first["request"]["method"], first["request"]["url"], _payload(first))
    assert again == expected

    # Different entries return different bodies, so a client that ignores the request fails.
    bodies = [
        json.dumps(_call(client, e["request"]["method"], e["request"]["url"], _payload(e)))
        for e in entries
    ]
    assert len(set(bodies)) == len(entries)


def test_fixture_client_get_json_matches_params(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"page": "a"}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"page": "b"}), encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"page": "c"}), encoding="utf-8")
    url = "https://example.test/api/items"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "family": "items",
                        "file": "a.json",
                        "request": {"method": "GET", "url": url, "params": {"x": 1, "y": "2"}},
                        "season": None,
                        "expect": {"page": "a"},
                    },
                    {
                        "family": "items",
                        "file": "b.json",
                        "request": {"method": "GET", "url": url, "params": {"x": 2, "y": "2"}},
                        "season": None,
                        "expect": {"page": "b"},
                    },
                    {
                        "family": "items",
                        "file": "c.json",
                        "request": {"method": "POST", "url": url, "body": {"x": 1, "y": "2"}},
                        "season": None,
                        "expect": {"page": "c"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    client = FixtureClient(manifest)
    assert client.get_json(url, {"y": "2", "x": 1}) == {"page": "a"}
    assert client.get_json(url, params={"x": 2, "y": "2"}, refresh=True) == {"page": "b"}
    assert client.post_json(url, {"y": "2", "x": 1}) == {"page": "c"}
    assert client.calls == 3
    for params in ({"x": 3, "y": "2"}, {"x": 1}, {"x": "1", "y": "2"}, None):
        with pytest.raises(LookupError):
            client.get_json(url, params)
    assert client.calls == 7


def test_fixture_client_rejects_unrecorded_requests():
    entry = next(e for e in _entries() if e["request"]["method"] == "POST")
    url = entry["request"]["url"]
    body = entry["request"]["body"]
    client = FixtureClient(MANIFEST)

    other_year = json.loads(json.dumps(body))
    variables = other_year["variables"]
    year_key = next(k for k in variables if k == "year" or k.endswith("Year"))
    variables[year_key] = "1901" if isinstance(variables[year_key], str) else 1901
    unrecorded = [
        ("POST", url, other_year),
        ("POST", url + "/other", body),
        ("POST", url, {**body, "operationName": "SomethingElse"}),
        ("POST", url, {}),
        ("GET", url, body),
    ]
    for method, req_url, payload in unrecorded:
        with pytest.raises(LookupError):
            _call(client, method, req_url, payload)
    assert client.calls == len(unrecorded)
    # The recorded request still works after misses.
    assert client.post_json(url, body)


@pytest.mark.parametrize("fail_after", [0, 1, 3])
def test_fixture_client_fail_after_raises_connection_error(fail_after):
    entries = _entries()
    client = FixtureClient(MANIFEST, fail_after=fail_after)
    for i in range(fail_after):
        entry = entries[i % len(entries)]
        assert _call(client, entry["request"]["method"], entry["request"]["url"], _payload(entry))
    for _ in range(2):  # the connection stays down
        entry = entries[0]
        with pytest.raises(ConnectionError):
            _call(client, entry["request"]["method"], entry["request"]["url"], _payload(entry))
    assert client.calls == fail_after + 2


def test_fixture_client_without_fail_after_never_fails():
    entries = _entries()
    client = FixtureClient(MANIFEST)
    for _ in range(3):
        for entry in entries:
            _call(client, entry["request"]["method"], entry["request"]["url"], _payload(entry))
    assert client.calls == 3 * len(entries)


def _keys_and_strings(value, keys: set[str], strings: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            _keys_and_strings(v, keys, strings)
    elif isinstance(value, list):
        for v in value:
            _keys_and_strings(v, keys, strings)
    elif isinstance(value, str):
        strings.append(value)


def test_fixtures_hold_no_personal_info():
    entries = _entries()
    assert any(e["family"] == "player_bio" for e in entries)
    for entry in entries:
        name = entry["file"]
        query = (entry["request"].get("body") or {}).get("query")
        if query:
            asked = sorted(set(PERSONAL_FIELD_TOKEN.findall(query)))
            assert not asked, f"{name} was recorded with a query asking for {asked}"

        keys: set[str] = set()
        strings: list[str] = []
        _keys_and_strings(
            json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8")), keys, strings
        )
        held = sorted(keys & set(PERSONAL_FIELDS))
        assert not held, f"{name} holds personal fields {held}"
        if entry["family"] == "player_bio":
            for text in strings:
                assert not RELATIVE_WORDS.search(text), (
                    f"{name} has family narrative: {text[:60]!r}"
                )


def test_fixtures_hold_no_secrets():
    files = sorted(p for p in FIXTURE_DIR.rglob("*") if p.is_file())
    assert MANIFEST in files and len(files) > 1
    key = os.environ.get("PGATOUR_API_KEY", "").strip()
    for path in files:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in SECRET_MARKERS:
            assert marker not in lowered, f"{path.relative_to(FIXTURE_DIR)} contains {marker!r}"
        assert not APPSYNC_KEY.search(lowered), f"{path.relative_to(FIXTURE_DIR)} has a key"
        if key:
            assert key not in text, f"{path.relative_to(FIXTURE_DIR)} contains PGATOUR_API_KEY"

    if key:
        for path in Path(__file__).parent.glob("*.py"):
            assert key not in path.read_text(encoding="utf-8"), path.name
