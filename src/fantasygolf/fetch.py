"""The polite fetcher: a raw response cache, a throttle, retries with backoff, a clear User-Agent.

`RawCache` stores each successful response once under `<root>/<source>/<key[:2]>/<key>.json`;
`PoliteClient` reads it first, so nothing is fetched twice. Request headers never reach the
cache: they are excluded from the key and from the stored file.
"""

import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from fantasygolf import __version__

DEFAULT_USER_AGENT = f"fantasygolf/{__version__} (personal research; low rate)"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    """A request failed for good; `status` is None when no response came back."""

    def __init__(self, url: str, status: int | None, detail: str) -> None:
        self.url = url
        self.status = status
        super().__init__(f"{url}: {detail}")


class RawCache:
    """Raw response bodies on disk, one JSON file per request."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def key(
        self, method: str, url: str, params: dict | None = None, body: dict | None = None
    ) -> str:
        """sha256 of the canonical request; dict order does not matter, headers are not part."""
        canonical = json.dumps(
            {"method": method.upper(), "url": url, "params": params or {}, "body": body},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def path(self, source: str, key: str) -> Path:
        return self.root / source / key[:2] / f"{key}.json"

    def get(self, source: str, key: str) -> dict | None:
        """The stored body, or None on a miss."""
        path = self.path(source, key)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["body"]

    def put(self, source: str, key: str, request: dict, status: int, body: dict) -> Path:
        """Write atomically: a temp file in the same directory, then a rename over the target."""
        path = self.path(source, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "request": request,
            "fetched_at": datetime.now(UTC).isoformat(),
            "status": status,
            "body": body,
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


class PoliteClient:
    """A JSON client for one source that throttles, retries and caches every success."""

    def __init__(
        self,
        source: str,
        cache: RawCache,
        *,
        user_agent: str | None = None,
        headers: dict | None = None,
        min_interval: float = 2.0,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        max_backoff: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.source = source
        self.cache = cache
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.clock = clock
        self.sleep = sleep
        self.network_calls = 0
        self._last_call: float | None = None
        self._http = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent or DEFAULT_USER_AGENT, **(headers or {})},
            timeout=30.0,
        )

    def get_json(self, url: str, params: dict | None = None, *, refresh: bool = False) -> dict:
        return self._fetch("GET", url, params=params, body=None, refresh=refresh)

    def post_json(self, url: str, body: dict, *, refresh: bool = False) -> dict:
        return self._fetch("POST", url, params=None, body=body, refresh=refresh)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _fetch(
        self, method: str, url: str, *, params: dict | None, body: dict | None, refresh: bool
    ) -> dict:
        key = self.cache.key(method, url, params, body)
        if not refresh:
            cached = self.cache.get(self.source, key)
            if cached is not None:
                return cached
        status, payload = self._request_with_retries(method, url, params, body)
        request = {"method": method, "url": url, "params": params, "body": body}
        self.cache.put(self.source, key, request, status, payload)
        return payload

    def _request_with_retries(
        self, method: str, url: str, params: dict | None, body: dict | None
    ) -> tuple[int, dict]:
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.network_calls += 1
            try:
                response = self._http.request(method, url, params=params, json=body)
            except httpx.TransportError as exc:
                status, detail, retry_after = None, f"{type(exc).__name__}: {exc}", 0.0
            else:
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        return status, response.json()
                    except ValueError as exc:
                        raise FetchError(url, status, f"status {status}, body is not JSON") from exc
                detail = f"status {status}"
                if status not in RETRYABLE_STATUSES:
                    raise FetchError(url, status, detail)
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            if attempt == self.max_retries:
                break
            wait = max(retry_after, self.backoff_base * 2**attempt)
            self.sleep(min(wait, self.max_backoff))
        attempts = self.max_retries + 1
        raise FetchError(url, status, f"{detail} after {attempts} attempts")

    def _throttle(self) -> None:
        """Keep at least `min_interval` between network calls, retries included."""
        if self._last_call is not None:
            wait = self.min_interval - (self.clock() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()


def _retry_after_seconds(value: str | None) -> float:
    """Retry-After as seconds: a number, or an HTTP date relative to now; 0 when absent or bad."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
