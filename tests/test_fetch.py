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
