"""Local search index: every PyPI project with its summary and keywords, in SQLite FTS5.

PyPI has no public full-text search API and pypi.org/search sits behind a browser challenge,
so pipseek keeps its own index:

1. The full index (name, summary, keywords for every project), rebuilt daily by this repo's
   GitHub Action (see ``pipseek.build_index``) and published as a release asset.
2. If that can't be downloaded: every project name from the PyPI Simple API (PEP 691), plus
   summaries for the ~15k most popular projects bundled with pipseek.

Download counts come from hugovk/top-pypi-packages and drive popularity-aware ranking.
Everything is stored in a local SQLite database, so searches take milliseconds and work offline.
"""

from __future__ import annotations

import difflib
import gzip
import io
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

import requests

from pipseek.build_index import parse_lines
from pipseek.cache import Cache

INDEX_URL = os.environ.get(
    "PIPSEEK_INDEX_URL",
    "https://github.com/ohmybahgosh/pipseek/releases/download/index/pypi-index.tsv.gz",
)
SIMPLE_URL = "https://pypi.org/simple/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
TOP_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json"
)

DB_TTL = 7 * 24 * 3600
TOP_TTL = 7 * 24 * 3600
SCHEMA_VERSION = "1"
EMPTY_SUMMARY_PENALTY = 150
PHRASE_BONUS = 80

_NORMALIZE_RE = re.compile(r"[-_.]+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

ProgressFn = Callable[[str], None]
Record = tuple[str, str, str]  # name, summary, keywords


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return _NORMALIZE_RE.sub("-", name).lower()


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Match:
    name: str
    score: float
    downloads: Optional[int] = None
    summary: str = ""


class IndexUnavailable(RuntimeError):
    """Raised when no package index can be downloaded or found in cache."""


class PackageIndex:
    """SQLite-backed index of PyPI projects with name and full-text search."""

    def __init__(self, conn: sqlite3.Connection, downloads: Optional[dict[str, int]] = None):
        self.conn = conn
        self.downloads = downloads or {}
        self._lock = threading.Lock()
        self._count: Optional[int] = None

    # Construction ---------------------------------------------------------------------------

    @classmethod
    def from_records(
        cls,
        records: Iterable[Record],
        downloads: Optional[dict[str, int]] = None,
        path: str = ":memory:",
        source: str = "memory",
    ) -> PackageIndex:
        conn = _connect(path)
        _build(conn, records, source)
        return cls(conn, {normalize(k): v for k, v in (downloads or {}).items()})

    @classmethod
    def load(
        cls,
        session: Optional[requests.Session] = None,
        cache: Optional[Cache] = None,
        refresh: bool = False,
        progress: Optional[ProgressFn] = None,
        timeout: float = 60,
    ) -> PackageIndex:
        session = session or requests.Session()
        cache = cache or Cache()
        db_path = cache.root / "index.sqlite"
        downloads = load_downloads(session, cache, refresh, progress, timeout)

        if db_path.exists() and not refresh:
            conn = _open_existing(db_path)
            if conn is not None:
                built = float(_meta(conn, "built_at") or 0)
                if time.time() - built < DB_TTL:
                    return cls(conn, downloads)
                conn.close()

        try:
            _rebuild_db(db_path, session, progress, timeout)
        except IndexUnavailable:
            conn = _open_existing(db_path) if db_path.exists() else None
            if conn is None:
                raise
            if progress:
                progress("Could not refresh the index, using the cached copy.")
            return cls(conn, downloads)

        conn = _open_existing(db_path)
        if conn is None:
            raise IndexUnavailable("The local index could not be opened.")
        return cls(conn, downloads)

    # Info -----------------------------------------------------------------------------------

    def __len__(self) -> int:
        if self._count is None:
            with self._lock:
                self._count = self.conn.execute("SELECT count(*) FROM packages").fetchone()[0]
        return self._count

    @property
    def source(self) -> str:
        return _meta(self.conn, "source") or "unknown"

    # Scoring --------------------------------------------------------------------------------

    def name_score(self, query: str, norm_name: str) -> float:
        q_tokens = tokens(normalize(query))
        if not q_tokens:
            return 0.0
        name_tokens = tokens(norm_name)
        joined_q = "-".join(q_tokens)
        multi = len(q_tokens) > 1

        score = 0.0
        if norm_name == joined_q:
            score += 250 if multi else 600
        elif norm_name.startswith(joined_q):
            score += 100 if multi else 200
        elif len(joined_q) >= 4 and joined_q in norm_name:
            score += 80

        hits = 0
        for qt in q_tokens:
            if qt in name_tokens:
                score += 40 if multi else 60
                hits += 1
            elif any(nt.startswith(qt) for nt in name_tokens):
                score += 25
                hits += 1
            elif len(qt) >= 4 and qt in norm_name:
                score += 10
                hits += 1
        if hits == 0:
            return 0.0
        score *= hits / len(q_tokens)
        score -= min(max(len(norm_name) - len(joined_q), 0), 40) * 1.5
        return max(score, 0.0)

    def popularity(self, norm_name: str) -> float:
        dl = self.downloads.get(norm_name)
        return math.log10(dl + 1) * 50 if dl else 0.0

    # Searching ------------------------------------------------------------------------------

    def search(self, query: str, limit: Optional[int] = None) -> list[Match]:
        q_tokens = tokens(normalize(query))
        if not q_tokens:
            return []

        found: dict[int, list] = {}  # id -> [name, norm, summary, text_score]

        with self._lock:
            # 1) Names containing every token.
            where = " AND ".join("norm LIKE ?" for _ in q_tokens)
            params = [f"%{t}%" for t in q_tokens]
            for rid, name, norm, summary in self.conn.execute(
                f"SELECT id, name, norm, summary FROM packages WHERE {where} LIMIT 20000", params
            ):
                found[rid] = [name, norm, summary or "", 0.0]

            # 2) Full-text over name, summary and keywords.
            for match_expr, penalty, cap in self._fts_queries(q_tokens):
                rows = self.conn.execute(
                    "SELECT p.id, p.name, p.norm, p.summary, bm25(fts, 0.5, 1.0, 1.0) "
                    "FROM fts JOIN packages p ON p.id = fts.rowid "
                    "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                    (match_expr, cap),
                ).fetchall()
                for rid, name, norm, summary, bm in rows:
                    text = min(-bm, 20.0) * 15 - penalty
                    entry = found.get(rid)
                    if entry is None:
                        found[rid] = [name, norm, summary or "", text]
                    elif text > entry[3]:
                        entry[3] = text
                if len(rows) >= 50:
                    break

        phrase = " ".join(q_tokens) if len(q_tokens) > 1 else None
        results = []
        for name, norm, summary, text in found.values():
            base = self.name_score(query, norm) + text
            if phrase and phrase in " ".join(tokens(summary)):
                base += PHRASE_BONUS
            if base <= 0:
                continue
            if not summary:
                base -= EMPTY_SUMMARY_PENALTY
            # Popularity amplifies relevance; it can't rescue a weak match on its own.
            score = base + self.popularity(norm) * min(1.0, max(base, 0) / 120) ** 0.5
            results.append(Match(name, score, self.downloads.get(norm), summary))
        results.sort(key=lambda m: (-m.score, m.name.lower()))
        return results[:limit] if limit else results

    @staticmethod
    def _fts_queries(q_tokens: list[str]) -> Iterator[tuple[str, float, int]]:
        quoted = [f'"{t}"*' if len(t) >= 5 else f'"{t}"' for t in q_tokens]
        yield " AND ".join(quoted), 0.0, 5000
        if len(q_tokens) > 1:
            yield " OR ".join(quoted), 120.0, 2000

    def suggest(self, query: str, n: int = 5) -> list[str]:
        """Typo-tolerant suggestions, drawn from popular packages first."""
        q = normalize(query)
        found = difflib.get_close_matches(q, list(self.downloads), n=n, cutoff=0.7)
        if len(found) < n and len(q) >= 2:
            with self._lock:
                pool = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT norm FROM packages WHERE norm LIKE ? LIMIT 50000", (q[:2] + "%",)
                    )
                ]
            extra = difflib.get_close_matches(q, pool, n=n, cutoff=0.8)
            found += [e for e in extra if e not in found]
        names = []
        with self._lock:
            for norm in found[:n]:
                row = self.conn.execute(
                    "SELECT name FROM packages WHERE norm = ?", (norm,)
                ).fetchone()
                names.append(row[0] if row else norm)
        return names


# SQLite helpers -----------------------------------------------------------------------------


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _open_existing(path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return None
    if _meta(conn, "schema") != SCHEMA_VERSION:
        conn.close()
        return None
    return conn


def _build(conn: sqlite3.Connection, records: Iterable[Record], source: str) -> int:
    conn.executescript(
        """
        DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS fts;
        DROP TABLE IF EXISTS packages;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE packages (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, norm TEXT NOT NULL,
            summary TEXT, keywords TEXT
        );
        CREATE VIRTUAL TABLE fts USING fts5(
            name, summary, keywords,
            content='packages', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2', detail=column
        );
        """
    )
    rows = ((name, normalize(name), summary, keywords) for name, summary, keywords in records)
    conn.executemany(
        "INSERT INTO packages (name, norm, summary, keywords) VALUES (?, ?, ?, ?)", rows
    )
    conn.execute("CREATE INDEX packages_norm ON packages(norm)")
    conn.execute("INSERT INTO fts(fts) VALUES ('rebuild')")
    count = conn.execute("SELECT count(*) FROM packages").fetchone()[0]
    conn.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [
            ("schema", SCHEMA_VERSION),
            ("source", source),
            ("built_at", str(time.time())),
            ("count", str(count)),
        ],
    )
    conn.commit()
    return count


def _rebuild_db(
    db_path: Path,
    session: requests.Session,
    progress: Optional[ProgressFn],
    timeout: float,
) -> None:
    records, source = _full_records(session, progress, timeout)
    if records is None:
        records, source = _fallback_records(session, progress, timeout), "names"

    if progress:
        progress("Building the local search database (about 10 seconds)...")
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = _connect(tmp)
    try:
        _build(conn, records, source)
    finally:
        conn.close()
    os.replace(tmp, db_path)


def _full_records(
    session: requests.Session, progress: Optional[ProgressFn], timeout: float
) -> tuple[Optional[list[Record]], str]:
    """Download the prebuilt full index (names + summaries + keywords)."""
    if progress:
        progress("Downloading the pipseek search index...")
    try:
        resp = session.get(INDEX_URL, timeout=timeout)
        if resp.status_code != 200:
            return None, ""
        text = gzip.decompress(resp.content).decode("utf-8")
    except (requests.RequestException, OSError, EOFError, UnicodeDecodeError):
        return None, ""
    records = [(name, summary, kw) for name, _, summary, kw in parse_lines(io.StringIO(text))]
    if len(records) < 100_000:  # a partial or broken asset; don't trust it
        return None, ""
    # Fill gaps (projects the builder hasn't reached yet) from the bundled summaries.
    bundled = {normalize(n): (s, k) for n, s, k in bundled_records()}
    records = [
        (n, s, k) if s or k else (n, *bundled.get(normalize(n), ("", ""))) for n, s, k in records
    ]
    return records, "full"


def _fallback_records(
    session: requests.Session, progress: Optional[ProgressFn], timeout: float
) -> list[Record]:
    """All project names from PyPI, with bundled summaries for popular projects."""
    if progress:
        progress("Downloading the PyPI project list (about 10 MB)...")
    try:
        resp = session.get(SIMPLE_URL, headers={"Accept": SIMPLE_ACCEPT}, timeout=timeout)
        resp.raise_for_status()
        names = [p["name"] for p in resp.json().get("projects", [])]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise IndexUnavailable(f"Could not download the PyPI index: {exc}") from exc
    bundled = {normalize(n): (s, k) for n, s, k in bundled_records()}
    return [(n, *bundled.get(normalize(n), ("", ""))) for n in names]


def bundled_records() -> list[Record]:
    """Summaries for the most popular projects, shipped inside the package."""
    try:
        data = resources.files("pipseek").joinpath("data/popular.tsv.gz").read_bytes()
    except (FileNotFoundError, OSError):
        return []
    text = gzip.decompress(data).decode("utf-8")
    return [(name, summary, kw) for name, _, summary, kw in parse_lines(io.StringIO(text))]


def load_downloads(
    session: requests.Session,
    cache: Cache,
    refresh: bool = False,
    progress: Optional[ProgressFn] = None,
    timeout: float = 30,
) -> dict[str, int]:
    """30-day download counts for the ~15k most popular projects, keyed by normalized name."""
    key = "top-packages"
    if not refresh:
        cached = cache.get(key, TOP_TTL)
        if cached:
            return cached
    try:
        if progress:
            progress("Fetching download statistics...")
        resp = session.get(TOP_URL, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        data = {normalize(r["project"]): int(r["download_count"]) for r in rows}
    except (requests.RequestException, ValueError, KeyError):
        stale = cache.load(key)
        return (stale or {}).get("data") or {}
    cache.set(key, data)
    return data
