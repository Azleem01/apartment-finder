"""Tiny JSON-file cache with per-entry freshness, committed to the repo so the
daily Action reuses yesterday's geocoding/commute work instead of re-querying."""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path


class JsonCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text() or "{}")
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get_fresh(self, key: str, max_age_days: int):
        """Return the entry if present and younger than max_age_days, else None."""
        entry = self.data.get(key)
        if not isinstance(entry, dict):
            return None
        fetched = entry.get("_fetched")
        if fetched:
            try:
                age = (dt.date.today() - dt.date.fromisoformat(fetched)).days
                if age > max_age_days:
                    return None
            except ValueError:
                return None
        return entry

    def put(self, key: str, value: dict) -> None:
        value = dict(value)
        value["_fetched"] = dt.date.today().isoformat()
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0, sort_keys=True))
