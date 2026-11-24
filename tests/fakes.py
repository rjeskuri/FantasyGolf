"""Test doubles shared by the ingest tests.

`FixtureClient` stands in for the HTTP client that talks to the pgatour.com API. It serves the
response bodies recorded under `tests/fixtures/pgatour/`, looked up by the request that
`manifest.json` says produced them, so an ingest test runs offline and sees exactly the data CI
sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _canonical(payload: Any) -> str:
    """The request payload as canonical JSON, so dict key order does not matter."""
    if payload is None:
        payload = {}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _key(method: str, url: str, payload: Any) -> tuple[str, str, str]:
    return (method.upper(), url, _canonical(payload))


class FixtureClient:
    """Serves recorded fixtures for the requests listed in a manifest.

    - A request matches an entry on method, url and params (GET) or body (POST); params and
      bodies compare by value, so key order is irrelevant.
    - Every call counts in `.calls`, including calls that raise.
    - With `fail_after=n`, the first n calls behave normally and every later call raises
      `ConnectionError` (the connection stays down), which lets a test interrupt an ingest
      part-way and resume it.
    - A request with no recorded response raises `LookupError`.
    - Each call returns a fresh copy of the body, so a caller mutating it cannot change what the
      next call sees. `refresh` is accepted for interface parity and ignored.
    """

    def __init__(self, manifest_path: Path, fail_after: int | None = None) -> None:
        self.manifest_path = Path(manifest_path)
        self.fail_after = fail_after
        self.calls = 0
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._files: dict[tuple[str, str, str], Path] = {}
        for entry in manifest["fixtures"]:
            request = entry["request"]
            method = request["method"].upper()
            payload = request.get("params") if method == "GET" else request.get("body")
            self._files[_key(method, request["url"], payload)] = (
                self.manifest_path.parent / entry["file"]
            )

    def get_json(self, url: str, params: dict | None = None, *, refresh: bool = False) -> dict:
        return self._serve("GET", url, params)

    def post_json(self, url: str, body: dict, *, refresh: bool = False) -> dict:
        return self._serve("POST", url, body)

    def _serve(self, method: str, url: str, payload: Any) -> dict:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError(
                f"FixtureClient: simulated connection failure on call {self.calls} "
                f"(fail_after={self.fail_after})"
            )
        path = self._files.get(_key(method, url, payload))
        if path is None:
            raise LookupError(
                f"FixtureClient: no recorded response for {method} {url} {_canonical(payload)}"
            )
        return json.loads(path.read_text(encoding="utf-8"))
