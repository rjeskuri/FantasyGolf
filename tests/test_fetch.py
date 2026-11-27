"""The polite fetcher: raw response cache, throttle, retries with backoff, a clear User-Agent.

Every test drives `PoliteClient` through `httpx.MockTransport` with a fake clock whose time only
moves when the injected `sleep` is called, so waits are measured on the injected clock and no
test sleeps for real.
"""

# noqa: I001 until fantasygolf.fetch exists, ruff files it under third-party imports.
from __future__ import annotations  # noqa: I001

import json
import re
from pathlib import Path

import httpx
import pytest

import fantasygolf
from fantasygolf.fetch import FetchError, PoliteClient, RawCache

BASE = "https://api.example.test"
URL = f"{BASE}/x"
RETRYABLE = [429, 500, 502, 503, 504]


class FakeClock:
    """A monotonic clock that advances only when `sleep` is called."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0, f"negative sleep {seconds}"
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """A MockTransport handler that records each request and the fake time it arrived."""

    def __init__(self, clock: FakeClock, responder=None) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []
        self.responder = responder or self._echo

    def _echo(self, request: httpx.Request, n: int) -> httpx.Response:
        payload = {"n": n, "method": request.method, "url": str(request.url)}
        if request.content:
            payload["sent"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock())
        return self.responder(request, len(self.requests))

    @property
    def calls(self) -> int:
        return len(self.requests)

    def gaps(self) -> list[float]:
        return [b - a for a, b in zip(self.times, self.times[1:], strict=False)]


def make_client(tmp_path: Path, responder=None, *, source: str = "pgatour", **kwargs):
    clock = FakeClock()
    server = Server(clock, responder)
    cache = RawCache(tmp_path / "raw")
    kwargs.setdefault("min_interval", 0.0)
    client = PoliteClient(
        source,
        cache,
        transport=httpx.MockTransport(server),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )
    return client, server, clock, cache


def cached_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def status_then_ok(status: int, failures: int = 1, headers: dict | None = None):
    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if n <= failures:
            return httpx.Response(status, headers=headers or {}, json={"error": status})
        return httpx.Response(200, json={"ok": True, "n": n})

    return respond


def always(status: int):
    def respond(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(status, json={"error": status, "n": n})

    return respond


# --- cache: a cached request makes zero network calls ---------------------------------------


def test_second_get_is_served_from_cache(tmp_path):
    client, server, _, _ = make_client(tmp_path)

    first = client.get_json(URL, {"season": 2019})
    second = client.get_json(URL, {"season": 2019})

    assert server.calls == 1
    assert client.network_calls == 1
    assert first == second
    assert first["n"] == 1 and "season=2019" in first["url"]

    # A different request is a miss; repeating it is a hit again.
    other = client.get_json(URL, {"season": 2020})
    assert other["n"] == 2 and "season=2020" in other["url"]
    assert client.get_json(URL, {"season": 2020}) == other
    assert client.get_json(f"{BASE}/y")["n"] == 3
    assert client.get_json(f"{BASE}/y")["n"] == 3
    assert server.calls == 3
    assert client.network_calls == 3


def test_cache_survives_a_new_client_on_the_same_root(tmp_path):
    client, _, _, _ = make_client(tmp_path)
    body = client.get_json(URL, {"season": 2021})
    cache_root = tmp_path / "raw"

    clock = FakeClock()
    fresh_server = Server(clock)
    fresh = PoliteClient(
        "pgatour",
        RawCache(cache_root),
        transport=httpx.MockTransport(fresh_server),
        clock=clock,
        sleep=clock.sleep,
        min_interval=0.0,
    )
    assert cache_root.exists()
    assert fresh.get_json(URL, {"season": 2021}) == body
    assert fresh_server.calls == 0
    assert fresh.network_calls == 0


def test_params_order_does_not_change_the_cache_hit(tmp_path):
    client, server, _, _ = make_client(tmp_path)
    client.get_json(URL, {"season": 2019, "tour": "R"})
    client.get_json(URL, {"tour": "R", "season": 2019})
    assert server.calls == 1


def test_post_bodies_with_same_content_share_key(tmp_path):
    cache = RawCache(tmp_path / "raw")
    body_a = {"operationName": "q", "variables": {"season": 2019, "tour": "R"}, "x": [1, 2]}
    body_b = {"x": [1, 2], "variables": {"tour": "R", "season": 2019}, "operationName": "q"}

    key_a = cache.key("POST", URL, body=body_a)
    key_b = cache.key("POST", URL, body=body_b)
    assert key_a == key_b
    assert re.fullmatch(r"[0-9a-f]{64}", key_a), key_a

    different = [
        {**body_a, "variables": {"season": 2020, "tour": "R"}},
        {**body_a, "operationName": "other"},
        {**body_a, "x": [2, 1]},
        {},
    ]
    keys = {cache.key("POST", URL, body=b) for b in different}
    assert key_a not in keys
    assert len(keys) == len(different)

    # Method, url and params are part of the key too.
    assert cache.key("GET", URL, body=body_a) != key_a
    assert cache.key("POST", f"{BASE}/y", body=body_a) != key_a
    assert cache.key("GET", URL, {"a": 1, "b": 2}) == cache.key("GET", URL, {"b": 2, "a": 1})
    assert cache.key("GET", URL, {"a": 1}) != cache.key("GET", URL, {"a": 2})

    # Through the client: same content, different order, one network call; the body is sent.
    client, server, _, _ = make_client(tmp_path / "client")
    first = client.post_json(URL, body_a)
    second = client.post_json(URL, body_b)
    assert server.calls == 1
    assert client.network_calls == 1
    assert first == second
    assert first["sent"] == body_a
    third = client.post_json(URL, different[0])
    assert server.calls == 2
    assert third["sent"] == different[0]


def test_raw_cache_put_and_get_round_trip(tmp_path):
    root = tmp_path / "raw"
    cache = RawCache(root)
    key = cache.key("GET", URL, {"season": 2019})
    assert cache.get("pgatour", key) is None

    body = {"players": [{"id": "1", "name": "A"}], "season": 2019}
    request = {"method": "GET", "url": URL, "params": {"season": 2019}}
    path = cache.put("pgatour", key, request, 200, body)

    assert Path(path) == root / "pgatour" / key[:2] / f"{key}.json"
    assert cache.get("pgatour", key) == body
    assert cache.get("other-source", key) is None
    assert cache.get("pgatour", cache.key("GET", URL, {"season": 2020})) is None
    # Written atomically: only the final file is left behind, no temp files.
    assert cached_files(root) == [Path(path)]
    stored = json.loads(Path(path).read_text(encoding="utf-8"))
    assert stored["status"] == 200
    assert stored["body"] == body
    assert stored["request"] == request
    assert stored["fetched_at"]


# --- throttle --------------------------------------------------------------------------------


def test_throttle_spaces_network_calls(tmp_path):
    for i, min_interval in enumerate([0.5, 2.0, 5.0]):
        client, server, clock, _ = make_client(tmp_path / str(i), min_interval=min_interval)
        for season in range(2015, 2020):
            client.get_json(URL, {"season": season})
        # A cached repeat makes no call, so it adds no new arrival time.
        client.get_json(URL, {"season": 2015})

        assert server.calls == 5
        gaps = server.gaps()
        assert len(gaps) == 4
        for gap in gaps:
            assert gap >= min_interval - 1e-9, (min_interval, gaps)
        # Waits went through the injected sleep, not real time.
        assert sum(clock.sleeps) >= 4 * min_interval - 1e-9


# --- retries ---------------------------------------------------------------------------------


def test_retry_after_is_honored_then_succeeds(tmp_path):
    for retry_after in [7, 13]:
        client, server, clock, _ = make_client(
            tmp_path / str(retry_after),
            status_then_ok(429, headers={"Retry-After": str(retry_after)}),
            backoff_base=0.25,
            max_backoff=60.0,
        )
        result = client.get_json(URL, {"season": 2019})

        assert result == {"ok": True, "n": 2}
        assert server.calls == 2
        (gap,) = server.gaps()
        assert gap >= retry_after, (retry_after, gap, clock.sleeps)
        assert max(clock.sleeps) >= retry_after
        # The success is cached; the 429 was not.
        assert client.get_json(URL, {"season": 2019}) == result
        assert server.calls == 2


def test_each_retryable_status_is_retried_then_succeeds(tmp_path):
    for status in RETRYABLE:
        client, server, _, _ = make_client(
            tmp_path / str(status), status_then_ok(status, failures=2), backoff_base=0.5
        )
        assert client.get_json(URL) == {"ok": True, "n": 3}, status
        assert server.calls == 3, status


def test_server_errors_retry_with_backoff_then_raise(tmp_path):
    for max_retries in [2, 4]:
        root = tmp_path / str(max_retries)
        client, server, _, cache = make_client(
            root, always(503), max_retries=max_retries, backoff_base=1.0, max_backoff=1000.0
        )
        with pytest.raises(FetchError) as info:
            client.get_json(URL)

        assert server.calls == max_retries + 1
        gaps = server.gaps()
        assert len(gaps) == max_retries
        for attempt, gap in enumerate(gaps):
            assert gap >= 1.0 * 2**attempt - 1e-9, gaps
        assert all(b > a for a, b in zip(gaps, gaps[1:], strict=False)), gaps

        err = info.value
        assert err.url == URL
        assert err.status == 503
        assert URL in str(err)
        assert "503" in str(err)

        # Nothing cached: no files, and the next call goes to the network again.
        assert cached_files(root / "raw") == []
        assert cache.get("pgatour", cache.key("GET", URL)) is None


def test_last_status_is_reported_after_mixed_failures(tmp_path):
    statuses = [500, 503, 502]

    def respond(request, n):
        return httpx.Response(statuses[n - 1], json={})

    client, server, _, _ = make_client(tmp_path, respond, max_retries=2, backoff_base=0.1)
    with pytest.raises(FetchError) as info:
        client.post_json(URL, {"q": 1})
    assert server.calls == 3
    assert info.value.status == 502
    assert "502" in str(info.value)
    assert info.value.url == URL


def test_backoff_is_capped_at_max_backoff(tmp_path):
    client, server, _, _ = make_client(
        tmp_path, always(500), max_retries=5, backoff_base=10.0, max_backoff=25.0
    )
    with pytest.raises(FetchError):
        client.get_json(URL)
    gaps = server.gaps()
    assert len(gaps) == 5
    expected = [min(10.0 * 2**attempt, 25.0) for attempt in range(5)]  # 10, 20, 25, 25, 25
    for gap, want in zip(gaps, expected, strict=True):
        assert want - 1e-9 <= gap <= 25.0 + 1e-9, (gaps, expected)


def test_transport_errors_are_retried_then_succeed(tmp_path):
    def respond(request, n):
        if n == 1:
            raise httpx.ConnectError("connection refused", request=request)
        if n == 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"ok": True})

    client, server, _, _ = make_client(tmp_path, respond, backoff_base=0.5)
    assert client.get_json(URL) == {"ok": True}
    assert server.calls == 3


def test_transport_errors_exhausted_raise_fetch_error_with_no_status(tmp_path):
    def respond(request, n):
        raise httpx.ConnectError("connection refused", request=request)

    client, server, _, _ = make_client(tmp_path, respond, max_retries=3, backoff_base=0.5)
    with pytest.raises(FetchError) as info:
        client.get_json(URL)
    assert server.calls == 4
    assert info.value.status is None
    assert info.value.url == URL
    assert URL in str(info.value)
    assert cached_files(tmp_path / "raw") == []


def test_404_raises_without_retry_and_is_not_cached(tmp_path):
    for status in [404, 400, 403]:
        root = tmp_path / str(status)
        client, server, clock, cache = make_client(
            root, always(status), max_retries=5, backoff_base=1.0
        )
        with pytest.raises(FetchError) as info:
            client.get_json(URL, {"season": 2019})

        assert server.calls == 1, status
        assert info.value.status == status
        assert str(status) in str(info.value)
        assert URL in info.value.url
        assert sum(clock.sleeps) < 1.0, clock.sleeps
        assert cached_files(root / "raw") == []

        # Not cached: asking again hits the network again.
        with pytest.raises(FetchError):
            client.get_json(URL, {"season": 2019})
        assert server.calls == 2


# --- refresh ---------------------------------------------------------------------------------


def test_refresh_bypasses_cache_read(tmp_path):
    client, server, _, _ = make_client(tmp_path)

    first = client.get_json(URL, {"season": 2019})
    refreshed = client.get_json(URL, {"season": 2019}, refresh=True)
    assert server.calls == 2
    assert client.network_calls == 2
    assert first["n"] == 1
    assert refreshed["n"] == 2

    # The refresh still wrote: the next plain read sees the refreshed body, with no call.
    assert client.get_json(URL, {"season": 2019}) == refreshed
    assert server.calls == 2

    # Same for POST.
    p1 = client.post_json(URL, {"q": 1})
    p2 = client.post_json(URL, {"q": 1}, refresh=True)
    assert (p1["n"], p2["n"]) == (3, 4)
    assert client.post_json(URL, {"q": 1}) == p2
    assert server.calls == 4


# --- headers and User-Agent ------------------------------------------------------------------


def test_cache_file_has_no_headers(tmp_path):
    secret_name, secret_value = "X-Api-Key", "tok-9f3b2c71d"
    client, server, _, cache = make_client(tmp_path, headers={secret_name: secret_value})

    body = client.get_json(URL, {"season": 2019})

    # The header did reach the server, so its absence from the file is meaningful.
    sent = server.requests[0]
    assert sent.headers[secret_name] == secret_value
    default_ua = f"fantasygolf/{fantasygolf.__version__} (personal research; low rate)"
    assert sent.headers["User-Agent"] == default_ua

    files = cached_files(tmp_path / "raw")
    key = cache.key("GET", URL, {"season": 2019})
    assert files == [tmp_path / "raw" / "pgatour" / key[:2] / f"{key}.json"]
    text = files[0].read_text(encoding="utf-8")
    assert secret_value not in text
    assert secret_name.lower() not in text.lower()
    assert "fantasygolf/" not in text
    assert "user-agent" not in text.lower()

    stored = json.loads(text)
    assert set(stored) == {"request", "fetched_at", "status", "body"}
    assert stored["status"] == 200
    assert stored["body"] == body


def test_default_user_agent_names_the_tool_and_no_person(tmp_path):
    client, server, _, _ = make_client(tmp_path)
    client.get_json(URL)
    ua = server.requests[0].headers["User-Agent"]

    assert ua == f"fantasygolf/{fantasygolf.__version__} (personal research; low rate)"
    assert re.fullmatch(r"fantasygolf/\d+\.\d+\.\d+\S* \(personal research; low rate\)", ua)
    assert "@" not in ua
    assert not re.search(r"https?://|mailto:|\.(com|org|net|io)\b", ua)


def test_custom_user_agent_and_headers_do_not_split_the_cache(tmp_path):
    client, server, _, _ = make_client(tmp_path, user_agent="other-agent/9")
    first = client.get_json(URL)
    assert server.requests[0].headers["User-Agent"] == "other-agent/9"

    clock = FakeClock()
    second_server = Server(clock)
    second = PoliteClient(
        "pgatour",
        RawCache(tmp_path / "raw"),
        headers={"X-Other": "1"},
        transport=httpx.MockTransport(second_server),
        clock=clock,
        sleep=clock.sleep,
        min_interval=0.0,
    )
    assert second.get_json(URL) == first
    assert second_server.calls == 0


# --- review fixes (PR #23) -------------------------------------------------------------------


def graphql_errors_then_ok(failures: int):
    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if n <= failures:
            return httpx.Response(200, json={"data": None, "errors": [{"message": f"boom {n}"}]})
        return httpx.Response(200, json={"data": {"ok": True}, "n": n})

    return respond


def test_graphql_errors_are_retried_and_the_healthy_body_is_cached(tmp_path):
    for failures in [1, 3]:
        root = tmp_path / str(failures)
        client, server, clock, cache = make_client(
            root, graphql_errors_then_ok(failures), max_retries=5, backoff_base=0.5
        )
        result = client.post_json(URL, {"operationName": "q", "variables": {"season": 2019}})

        assert result == {"data": {"ok": True}, "n": failures + 1}, failures
        assert server.calls == failures + 1
        # Retried with backoff, like a 5xx.
        gaps = server.gaps()
        assert len(gaps) == failures
        for attempt, gap in enumerate(gaps):
            assert gap >= 0.5 * 2**attempt - 1e-9, gaps

        # Only the healthy body is on disk, and it serves the repeat with no call.
        files = cached_files(root / "raw")
        assert len(files) == 1
        stored = json.loads(files[0].read_text(encoding="utf-8"))
        assert stored["body"] == result
        assert "errors" not in json.dumps(stored["body"])
        again = client.post_json(URL, {"variables": {"season": 2019}, "operationName": "q"})
        assert again == result
        assert server.calls == failures + 1

    # An empty errors list is a healthy response: returned and cached at once.
    def empty_errors(request, n):
        return httpx.Response(200, json={"data": {"x": n}, "errors": []})

    client, server, _, _ = make_client(tmp_path / "empty", empty_errors)
    assert client.get_json(URL) == {"data": {"x": 1}, "errors": []}
    assert client.get_json(URL) == {"data": {"x": 1}, "errors": []}
    assert server.calls == 1


def test_graphql_errors_forever_raise_and_cache_nothing(tmp_path):
    for max_retries in [1, 3]:
        root = tmp_path / str(max_retries)
        client, server, _, cache = make_client(
            root, graphql_errors_then_ok(10_000), max_retries=max_retries, backoff_base=0.25
        )
        body = {"operationName": "q", "variables": {"season": 2020}}
        with pytest.raises(FetchError) as info:
            client.post_json(URL, body)

        assert server.calls == max_retries + 1
        assert info.value.status == 200
        assert info.value.url == URL
        assert cached_files(root / "raw") == []
        assert cache.get("pgatour", cache.key("POST", URL, body=body)) is None

        # Not cached: asking again goes back to the network.
        with pytest.raises(FetchError):
            client.post_json(URL, body)
        assert server.calls == 2 * (max_retries + 1)


def test_retry_after_is_never_shortened_by_max_backoff(tmp_path):
    for retry_after in [7, 13]:
        client, server, clock, _ = make_client(
            tmp_path / str(retry_after),
            status_then_ok(429, headers={"Retry-After": str(retry_after)}),
            backoff_base=0.25,
            max_backoff=5.0,
        )
        assert client.get_json(URL) == {"ok": True, "n": 2}
        (gap,) = server.gaps()
        assert gap >= retry_after - 1e-9, (retry_after, gap, clock.sleeps)

    # The cap still bounds the computed backoff when Retry-After is shorter than it.
    client, server, _, _ = make_client(
        tmp_path / "short",
        status_then_ok(503, headers={"Retry-After": "1"}),
        backoff_base=10.0,
        max_backoff=5.0,
    )
    assert client.get_json(URL) == {"ok": True, "n": 2}
    (gap,) = server.gaps()
    assert 5.0 - 1e-9 <= gap <= 5.0 + 1e-9, gap


def test_cache_key_follows_the_effective_request_url(tmp_path):
    # httpx 0.28: params={} drops the URL's own query, params={...} replaces it, None keeps it.
    url = f"{BASE}/x?season=2019"
    client, server, _, _ = make_client(tmp_path)

    calls = [
        (url, None),
        (url, {}),
        (url, {"tour": "R"}),
        (url, None),
        (url, {}),
        (url, {"tour": "R"}),
    ]
    with httpx.Client() as builder:
        for i, (u, params) in enumerate(calls):
            got = client.get_json(u, params)
            # The URL httpx sends for this exact call, built by an independent client.
            effective = str(builder.build_request("GET", u, params=params).url)
            assert got["url"] == effective, (i, params, got, effective)

    # Three distinct effective requests, each fetched once; the repeats were cache hits.
    sent = [str(r.url) for r in server.requests]
    assert len(set(sent)) == len(sent) == server.calls
    assert server.calls == 3


# Ruling: the key canonicalizes parameter-name order but keeps each repeated name's value order.
SAME_KEY = [
    ("?a=1&b=2", "?b=2&a=1"),
    ("?sort=x&a=1&sort=y", "?a=1&sort=x&sort=y"),
    ("?b=2&sort=x&a=1&sort=y", "?sort=x&sort=y&a=1&b=2"),
]
DIFFERENT_KEY = [
    ("?sort=score&sort=date", "?sort=date&sort=score"),
    ("?sort=x&a=1&sort=y", "?a=1&sort=y&sort=x"),
    ("?a=1&sort=x&sort=y&sort=z", "?a=1&sort=x&sort=z&sort=y"),
]


@pytest.mark.parametrize(("left", "right"), SAME_KEY)
def test_cache_key_ignores_parameter_name_order(tmp_path, left, right):
    cache = RawCache(tmp_path / "raw")
    assert cache.key("GET", URL + left) == cache.key("GET", URL + right)


@pytest.mark.parametrize(("left", "right"), DIFFERENT_KEY)
def test_cache_key_keeps_the_value_order_of_a_repeated_parameter(tmp_path, left, right):
    cache = RawCache(tmp_path / "raw")
    assert cache.key("GET", URL + left) != cache.key("GET", URL + right)


def test_cache_key_keeps_list_param_value_order(tmp_path):
    cache = RawCache(tmp_path / "raw")
    # A list value in `params` is sent in its own order, so the order is part of the key.
    assert cache.key("GET", URL, {"sort": ["score", "date"]}) != cache.key(
        "GET", URL, {"sort": ["date", "score"]}
    )
    assert cache.key("GET", URL, {"sort": ["score", "date"], "a": 1}) == cache.key(
        "GET", URL, {"a": 1, "sort": ["score", "date"]}
    )


def test_repeated_parameter_value_order_is_a_separate_cache_entry(tmp_path):
    client, server, _, _ = make_client(tmp_path)
    first_url = URL + "?sort=score&sort=date"
    second_url = URL + "?sort=date&sort=score"

    first = client.get_json(first_url)
    second = client.get_json(second_url)

    assert server.calls == 2
    assert client.network_calls == 2
    assert first["url"] == first_url
    assert second["url"] == second_url
    # Each is now cached under its own key: repeats make no call and return their own body.
    assert client.get_json(first_url)["url"] == first_url
    assert client.get_json(second_url)["url"] == second_url
    assert server.calls == 2


def test_concurrent_same_request_makes_one_network_call(tmp_path):
    import threading

    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            hits.append(str(request.url))
            first = len(hits) == 1
        if first:
            entered.set()
            release.wait(timeout=3.0)
        return httpx.Response(200, json={"url": str(request.url), "n": len(hits)})

    client = PoliteClient(
        "pgatour",
        RawCache(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
        min_interval=0.0,
        max_retries=0,
    )
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            results[name] = client.get_json(URL, {"season": 2019})
        except BaseException as exc:  # surfaced in the main thread below
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("a",), daemon=True)
    t1.start()
    assert entered.wait(timeout=3.0), "first request never reached the server"
    t2 = threading.Thread(target=worker, args=("b",), daemon=True)
    t2.start()
    # Give the second thread time to reach the network if nothing stops it.
    t2.join(timeout=0.3)
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    client.close()

    assert not t1.is_alive() and not t2.is_alive(), "a thread hung"
    assert errors == []
    assert len(hits) == 1, hits
    assert results["a"] == results["b"]
    assert results["a"]["n"] == 1 and "season=2019" in results["a"]["url"]


def test_every_5xx_but_501_and_505_is_retried(tmp_path):
    for status in [520, 507, 508, 599]:
        client, server, _, _ = make_client(
            tmp_path / f"ok-{status}", status_then_ok(status, failures=1), backoff_base=0.5
        )
        assert client.get_json(URL) == {"ok": True, "n": 2}, status
        assert server.calls == 2, status

    for status in [501, 505]:
        root = tmp_path / f"no-{status}"
        client, server, clock, _ = make_client(
            root, status_then_ok(status, failures=1), max_retries=5, backoff_base=1.0
        )
        with pytest.raises(FetchError) as info:
            client.get_json(URL)
        assert server.calls == 1, status
        assert info.value.status == status
        assert sum(clock.sleeps) < 1.0
        assert cached_files(root / "raw") == []


# --- stored-request check on cache hits (PR #23 round 3) --------------------------------------
# Ruling (owner): no legacy-key migration, since no cache predates this PR. A hit counts only when
# the file's stored request is the effective request being made and its stored response is a
# healthy 2xx; anything else is a miss: one network call, then the file at that path is rewritten.


def tagged(tag: str):
    """An echo responder whose bodies name the server they came from."""

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        payload = {"server": tag, "n": n, "method": request.method, "url": str(request.url)}
        if request.content:
            payload["sent"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return respond


def fresh_client(root: Path, tag: str):
    clock = FakeClock()
    server = Server(clock, tagged(tag))
    client = PoliteClient(
        "pgatour",
        RawCache(root),
        transport=httpx.MockTransport(server),
        clock=clock,
        sleep=clock.sleep,
        min_interval=0.0,
        max_retries=0,
    )
    return client, server


def call(client: PoliteClient, req: tuple):
    method, url, params, body = req
    if method == "GET":
        return client.get_json(url, params)
    return client.post_json(url, body)


def fetch_and_locate(root: Path, req: tuple, tag: str) -> tuple[dict, Path]:
    """Fetch `req` once into `root` and return its body and the one new cache file it wrote."""
    before = set(cached_files(root))
    client, _ = fresh_client(root, tag)
    body = call(client, req)
    (path,) = set(cached_files(root)) - before
    return body, path


def effective_url(req: tuple) -> str:
    method, url, params, body = req
    with httpx.Client() as builder:
        return str(builder.build_request(method, url, params=params, json=body).url)


# (request A, request B): B's valid cache file is planted at A's path.
MISMATCHED = [
    pytest.param(
        ("GET", URL, {"season": 2019}, None),
        ("GET", URL, {"season": 2020}, None),
        id="other-param-value",
    ),
    pytest.param(
        ("GET", URL, {"sort": ["date", "score"]}, None),
        ("GET", URL, {"sort": ["score", "date"]}, None),
        id="repeated-param-order",
    ),
    pytest.param(
        ("GET", f"{BASE}/x", None, None), ("GET", f"{BASE}/y", None, None), id="other-path"
    ),
    pytest.param(("POST", URL, None, {"q": 1}), ("GET", URL, None, None), id="other-method"),
    pytest.param(
        ("POST", URL, None, {"operationName": "q", "variables": {"season": 2019}}),
        ("POST", URL, None, {"operationName": "q", "variables": {"season": 2020}}),
        id="other-body",
    ),
]


@pytest.mark.parametrize(("req_a", "req_b"), MISMATCHED)
def test_cache_file_for_another_request_is_a_miss_and_is_rewritten(tmp_path, req_a, req_b):
    root = tmp_path / "raw"
    _, path_a = fetch_and_locate(root, req_a, "seed")
    body_b, path_b = fetch_and_locate(root, req_b, "seed")
    assert path_a != path_b
    # A's path now holds B's complete, valid record.
    path_a.write_text(path_b.read_text(encoding="utf-8"), encoding="utf-8")
    files_before = cached_files(root)

    client, server = fresh_client(root, "live")
    got = call(client, req_a)

    # A miss: one network call, and A's own response, never B's body.
    assert server.calls == 1
    assert client.network_calls == 1
    assert got != body_b
    assert got["server"] == "live"
    assert got["method"] == req_a[0]
    assert got["url"] == effective_url(req_a)
    if req_a[3] is not None:
        assert got["sent"] == req_a[3]

    # The file at A's path was overwritten in place with A's record, and B's file is untouched.
    assert cached_files(root) == files_before
    stored = json.loads(path_a.read_text(encoding="utf-8"))
    assert stored["status"] == 200
    assert stored["body"] == got
    assert json.loads(path_b.read_text(encoding="utf-8"))["body"] == body_b

    # A new client serves both from cache, each its own body, with zero network calls.
    later, later_server = fresh_client(root, "later")
    assert call(later, req_a) == got
    assert call(later, req_b) == body_b
    assert later_server.calls == 0
    assert later.network_calls == 0


@pytest.mark.parametrize("bad_request", [{}, None, "GET https://api.example.test/x"])
def test_cache_file_without_a_usable_stored_request_is_a_miss(tmp_path, bad_request):
    root = tmp_path / "raw"
    req = ("GET", URL, {"season": 2019}, None)
    _, path = fetch_and_locate(root, req, "seed")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["request"] = bad_request
    record["body"] = {"planted": True}
    path.write_text(json.dumps(record), encoding="utf-8")

    client, server = fresh_client(root, "live")
    got = call(client, req)
    assert server.calls == 1
    assert got["server"] == "live" and got["url"] == effective_url(req)
    assert json.loads(path.read_text(encoding="utf-8"))["body"] == got

    later, later_server = fresh_client(root, "later")
    assert call(later, req) == got
    assert later_server.calls == 0


# (first, second, an equivalent spelling of first). The stored-request check uses the key's
# canonical form: parameter-name order is no difference, so it must never refetch or thrash.
REPEATED_FORMS = [
    pytest.param(
        ("GET", URL + "?sort=score&sort=date", None, None),
        ("GET", URL + "?sort=date&sort=score", None, None),
        ("GET", URL, {"sort": ["score", "date"]}, None),
        id="query-string",
    ),
    pytest.param(
        ("GET", URL, {"sort": ["score", "date"]}, None),
        ("GET", URL, {"sort": ["date", "score"]}, None),
        ("GET", URL + "?sort=score&sort=date", None, None),
        id="list-param",
    ),
    pytest.param(
        ("GET", URL, {"sort": ["score", "date"], "tour": "R"}, None),
        ("GET", URL, {"tour": "R", "sort": ["date", "score"]}, None),
        ("GET", URL + "?tour=R&sort=score&sort=date", None, None),
        id="list-param-with-other-names",
    ),
]


@pytest.mark.parametrize(("first", "second", "first_again"), REPEATED_FORMS)
def test_repeated_param_orders_are_each_served_their_own_body(tmp_path, first, second, first_again):
    root = tmp_path / "raw"
    body_1, path_1 = fetch_and_locate(root, first, "seed")
    body_2, path_2 = fetch_and_locate(root, second, "seed")
    assert path_1 != path_2
    assert body_1["url"] == effective_url(first) and body_2["url"] == effective_url(second)

    # Swap the two files: each path now holds the other order's valid record, as if one hash had
    # been filled by the other request. Neither may be served the other's rows.
    text_1, text_2 = path_1.read_text(encoding="utf-8"), path_2.read_text(encoding="utf-8")
    path_1.write_text(text_2, encoding="utf-8")
    path_2.write_text(text_1, encoding="utf-8")

    client, server = fresh_client(root, "live")
    got_1 = call(client, first)
    got_2 = call(client, second)
    # An equivalent spelling of `first` is the same request: a hit on the rewritten file.
    assert call(client, first_again) == got_1
    assert server.calls == 2
    assert got_1["url"] == effective_url(first)
    assert got_2["url"] == effective_url(second)
    assert got_1["server"] == got_2["server"] == "live"

    later, later_server = fresh_client(root, "later")
    for _ in range(2):
        assert call(later, first) == got_1
        assert call(later, first_again) == got_1
        assert call(later, second) == got_2
    assert later_server.calls == 0
    assert later.network_calls == 0


# Stored responses that must never be served, even under the right request.
UNHEALTHY = [
    pytest.param(500, None, id="500"),
    pytest.param(503, None, id="503"),
    pytest.param(404, None, id="404"),
    pytest.param(429, None, id="429"),
    pytest.param(304, None, id="304"),
    pytest.param(200, {"data": None, "errors": [{"message": "boom"}]}, id="graphql-errors"),
]


@pytest.mark.parametrize(("status", "planted_body"), UNHEALTHY)
def test_cached_error_response_is_never_served(tmp_path, status, planted_body):
    root = tmp_path / "raw"
    req = ("POST", URL, None, {"operationName": "q", "variables": {"season": 2019}})
    _, path = fetch_and_locate(root, req, "seed")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status"] = status
    record["body"] = planted_body if planted_body is not None else {"error": status}
    path.write_text(json.dumps(record), encoding="utf-8")

    client, server = fresh_client(root, "live")
    got = call(client, req)
    assert server.calls == 1
    assert got["server"] == "live" and got["sent"] == req[3]

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["status"] == 200
    assert stored["body"] == got

    later, later_server = fresh_client(root, "later")
    assert call(later, req) == got
    assert later_server.calls == 0

    # A planted non-2xx status under a GET is refused too.
    if status != 200:
        get_req = ("GET", URL, {"season": 2019}, None)
        _, get_path = fetch_and_locate(root, get_req, "seed")
        record = json.loads(get_path.read_text(encoding="utf-8"))
        record["status"], record["body"] = status, {"error": status}
        get_path.write_text(json.dumps(record), encoding="utf-8")
        client, server = fresh_client(root, "live2")
        assert call(client, get_req)["server"] == "live2"
        assert server.calls == 1
