"""Where the pipeline keeps its data: database, raw JSON cache and reports."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    db: Path
    cache: Path
    reports: Path

    @classmethod
    def from_env(cls) -> "Paths":
        """Defaults sit under the working directory; FANTASYGOLF_* variables override them."""
        return cls(
            db=Path(os.environ.get("FANTASYGOLF_DB") or "data/fantasygolf.duckdb"),
            cache=Path(os.environ.get("FANTASYGOLF_CACHE") or "data/raw"),
            reports=Path(os.environ.get("FANTASYGOLF_REPORTS") or "reports"),
        )
