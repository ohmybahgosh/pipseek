"""Tiny on-disk cache for the package index, popularity data, and API responses."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional


def cache_dir() -> Path:
    """Return the per-user cache directory, honoring PIPSEEK_CACHE_DIR and platform norms."""
    override = os.environ.get("PIPSEEK_CACHE_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "pipseek"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "pipseek"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pipseek"
    base.mkdir(parents=True, exist_ok=True)
    return base


class Cache:
    """Gzipped JSON blobs keyed by name, each stored with a timestamp and optional ETag."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or cache_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = hashlib.sha1(key.encode()).hexdigest()[:16] if len(key) > 64 or "/" in key else key
        return self.root / f"{safe}.json.gz"

    def load(self, key: str) -> Optional[dict[str, Any]]:
        path = self._path(key)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def get(self, key: str, max_age: float) -> Optional[Any]:
        """Return cached data if younger than max_age seconds, else None."""
        entry = self.load(key)
        if entry and time.time() - entry.get("ts", 0) < max_age:
            return entry.get("data")
        return None

    def set(self, key: str, data: Any, etag: Optional[str] = None) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
                json.dump({"ts": time.time(), "etag": etag, "data": data}, fh)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def touch(self, key: str) -> None:
        """Mark an existing entry fresh (used after a 304 Not Modified)."""
        entry = self.load(key)
        if entry is not None:
            self.set(key, entry.get("data"), entry.get("etag"))

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.json.gz"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
