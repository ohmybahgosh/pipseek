"""Tiny on-disk cache for the package index, popularity data, and API responses."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import sys
import tempfile
import time
import zlib
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
                entry = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, EOFError, zlib.error):
            # Truncated or corrupt (an interrupted write, a full disk): drop it and refetch.
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        return entry if isinstance(entry, dict) else None

    def get(self, key: str, max_age: float) -> Optional[Any]:
        """Return cached data if younger than max_age seconds, else None."""
        entry = self.load(key)
        if entry and time.time() - entry.get("ts", 0) < max_age:
            return entry.get("data")
        return None

    def set(self, key: str, data: Any, etag: Optional[str] = None) -> None:
        path = self._path(key)
        payload = json.dumps({"ts": time.time(), "etag": etag, "data": data}).encode("utf-8")
        tmp = None
        try:
            # A unique temp file, so two pipseek runs never write to the same one.
            fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=self.root)
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(
                fileobj=raw, mode="wb", compresslevel=5
            ) as fh:
                fh.write(payload)
            os.replace(tmp, path)
            tmp = None
        except OSError:
            pass  # the cache is best-effort; a locked or full disk just means a refetch later
        finally:
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def touch(self, key: str) -> None:
        """Mark an existing entry fresh (used after a 304 Not Modified)."""
        entry = self.load(key)
        if entry is not None:
            self.set(key, entry.get("data"), entry.get("etag"))

    def clear(self) -> int:
        """Delete everything pipseek keeps here: the search database, cached API responses,
        and temp files left behind by interrupted runs."""
        removed = 0
        patterns = ("*.json.gz", "*.tmp", "index.sqlite", "index.sqlite-*")
        for pattern in patterns:
            for path in self.root.glob(pattern):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
