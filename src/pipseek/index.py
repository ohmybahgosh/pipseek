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

import contextlib
import difflib
import gzip
import hashlib
import io
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import BinaryIO, Callable, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

import requests

from pipseek.build_index import parse_lines
from pipseek.cache import Cache

DEFAULT_INDEX_URL = (
    "https://github.com/ohmybahgosh/pipseek/releases/download/index/pypi-index.tsv.gz"
)
SIMPLE_URL = "https://pypi.org/simple/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
TOP_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json"
)

CHECK_TTL = 24 * 3600  # once a day, ask whether the published index has changed
DB_TTL = 7 * 24 * 3600  # if that check can't be made, rebuild after a week anyway
FALLBACK_TTL = 24 * 3600  # retry for the full index daily when we only have the fallback
TOP_TTL = 7 * 24 * 3600
TOP_RETRY = 6 * 3600  # after a failed download-stats fetch, don't retry for this long
TOP_TIMEOUT = 15
MAX_INDEX_DOWNLOAD = 200 * 1024 * 1024  # the compressed index is about 35 MB today
MAX_INDEX_TEXT = 1024 * 1024 * 1024  # ...and about 150 MB uncompressed
MIN_INDEX_RECORDS = 100_000
INDEX_ATTEMPTS = 3
INDEX_RETRY_DELAY = 2.0
MIN_SUMMARY_SHARE = 0.5  # a healthy index has summaries for about 90% of projects
SCHEMA_VERSION = "1"
EMPTY_SUMMARY_PENALTY = 150
PHRASE_BONUS = 80

_NORMALIZE_RE = re.compile(r"[-_.]+")
_TOKEN_RE = re.compile(r"[^\W_]+")  # letters and digits in any script

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


class IndexRejected(Exception):
    """The downloaded index was too large, corrupt, or didn't match its checksum."""


def index_url() -> str:
    """Where to get the prebuilt index: PIPSEEK_INDEX_URL (http(s), file:// or a path)."""
    return os.environ.get("PIPSEEK_INDEX_URL") or DEFAULT_INDEX_URL


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
                if _still_current(conn, session, timeout):
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
            conn.close()
            _mark_checked(db_path)  # try again tomorrow, not on every run
            conn = _open_existing(db_path)
            if conn is None:
                raise
            return cls(conn, downloads)

        conn = _open_existing(db_path)
        if conn is None:
            raise IndexUnavailable("The local index could not be opened.")
        return cls(conn, downloads)

    @classmethod
    def open_cached(cls, cache: Optional[Cache] = None) -> Optional[PackageIndex]:
        """The index already on disk, without touching the network; None if there isn't one."""
        cache = cache or Cache()
        db_path = cache.root / "index.sqlite"
        if not db_path.exists():
            return None
        conn = _open_existing(db_path)
        if conn is None:
            return None
        downloads = (cache.load("top-packages") or {}).get("data") or {}
        return cls(conn, downloads)

    def close(self) -> None:
        self.conn.close()

    # Info -----------------------------------------------------------------------------------

    def __len__(self) -> int:
        if self._count is None:
            with self._lock:
                self._count = self.conn.execute("SELECT count(*) FROM packages").fetchone()[0]
        return self._count

    @property
    def source(self) -> str:
        return _meta(self.conn, "source") or "unknown"

    def get(self, name: str) -> Optional[Match]:
        """The index entry for one project, matched by normalized name."""
        norm = normalize(name)
        with self._lock:
            row = self.conn.execute(
                "SELECT name, summary FROM packages WHERE norm = ?", (norm,)
            ).fetchone()
        if row is None:
            return None
        return Match(row[0], 0.0, self.downloads.get(norm), row[1] or "")

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
    # Build into a uniquely named temp file next to the database, then swap it in, so two
    # pipseek runs never write to the same file and a reader never sees a half-built index.
    fd, tmp_name = tempfile.mkstemp(prefix="index-", suffix=".tmp", dir=db_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if not _build_full(tmp, session, progress, timeout):
            if _source_of(db_path) == "full":
                # Don't replace a good full index with the names-only fallback; keep it and
                # try again next run.
                raise IndexUnavailable("Could not download a fresh search index.")
            records = _fallback_records(session, progress, timeout)
            if progress:
                progress("Building the local search database (about 10 seconds)...")
            conn = _connect(tmp)
            try:
                _build(conn, records, "names")
            finally:
                conn.close()
        try:
            os.replace(tmp, db_path)
        except OSError as exc:
            # On Windows this fails while another pipseek has the database open.
            if not db_path.exists():
                raise IndexUnavailable(f"Could not save the search database: {exc}") from exc
            if progress:
                progress("Another pipseek is using the search database; keeping the current one.")
            _mark_checked(db_path)  # don't download again on every run while it's in use
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _still_current(conn: sqlite3.Connection, session: requests.Session, timeout: float) -> bool:
    """Whether the local database can be used as is.

    A full index is checked once a day: pipseek fetches the published SHA-256 (84 bytes) and
    only downloads the 35 MB index when it changed.  When the check can't be made, the local
    copy is used for up to a week.
    """
    now = time.time()
    built = float(_meta(conn, "built_at") or 0)
    if _meta(conn, "source") != "full":
        return now - built < FALLBACK_TTL
    checked = max(built, float(_meta(conn, "checked_at") or 0))
    if now - checked < CHECK_TTL:
        return True
    local = _meta(conn, "sha256")
    remote = _published_checksum(session, timeout)
    if remote is None:
        return now - built < DB_TTL  # can't tell; fall back to the weekly rebuild
    if local != remote:
        return False  # a newer index is out
    with contextlib.suppress(sqlite3.Error):
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('checked_at', ?)", (str(now),))
        conn.commit()
    return True


def _mark_checked(db_path: Path) -> None:
    """Record that the daily check ran, so the next one is a day away."""
    conn = _open_existing(db_path)
    if conn is None:
        return
    with contextlib.suppress(sqlite3.Error):
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('checked_at', ?)", (str(time.time()),))
        conn.commit()
    conn.close()


def _published_checksum(session: requests.Session, timeout: float) -> Optional[str]:
    url = index_url()
    if url.startswith(("http://", "https://")):
        return _fetch_checksum(session, url, min(timeout, 15))
    path = url2pathname(urlparse(url).path) if url.startswith("file:") else url
    with contextlib.suppress(OSError, IndexError):
        return Path(path + ".sha256").read_text().split()[0].lower()
    return None


def _source_of(db_path: Path) -> Optional[str]:
    if not db_path.exists():
        return None
    conn = _open_existing(db_path)
    if conn is None:
        return None
    try:
        return _meta(conn, "source")
    finally:
        conn.close()


class _Meter(io.RawIOBase):
    """Reads from a stream, stopping at a size limit and hashing what it read."""

    def __init__(self, raw, limit: int, digest=None) -> None:
        super().__init__()
        self.raw = raw
        self.limit = limit
        self.digest = digest
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.raw.read(len(buffer))
        n = len(data)
        self.count += n
        if self.count > self.limit:
            raise IndexRejected(f"the index is larger than {self.limit // (1024 * 1024)} MB")
        if self.digest is not None:
            self.digest.update(data)
        buffer[:n] = data
        return n


def _fetch_checksum(session: requests.Session, url: str, timeout: float) -> Optional[str]:
    try:
        resp = session.get(url + ".sha256", timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.text.strip():
        return None
    return resp.text.split()[0].lower()


def _open_index(
    session: requests.Session, timeout: float
) -> Optional[tuple[BinaryIO, Optional[str], Callable[[], None], Callable[[], Optional[str]]]]:
    """(binary stream, expected sha256 or None, close, re-read the checksum), or None."""
    url = index_url()
    if url.startswith(("http://", "https://")):
        expected = _fetch_checksum(session, url, timeout)
        # The daily job swaps the file in by deleting the old asset and renaming the new one,
        # which leaves a moment with no file.  A short retry rides over that.
        for attempt in range(INDEX_ATTEMPTS):
            resp = session.get(url, timeout=timeout, stream=True)
            if resp.status_code == 200:
                break
            resp.close()
            if resp.status_code != 404 and resp.status_code < 500:
                return None
            if attempt + 1 < INDEX_ATTEMPTS:
                time.sleep(INDEX_RETRY_DELAY)
        else:
            return None
        if int(resp.headers.get("Content-Length") or 0) > MAX_INDEX_DOWNLOAD:
            resp.close()
            return None
        resp.raw.decode_content = True
        return resp.raw, expected, resp.close, lambda: _fetch_checksum(session, url, timeout)
    # A local file, as a path or a file:// URL.
    path = url2pathname(urlparse(url).path) if url.startswith("file:") else url

    def local_checksum() -> Optional[str]:
        with contextlib.suppress(OSError, IndexError):
            return Path(path + ".sha256").read_text().split()[0].lower()
        return None

    fh = open(path, "rb")  # noqa: SIM115 - closed by the caller
    return fh, local_checksum(), fh.close, local_checksum


def _build_full(
    tmp: Path, session: requests.Session, progress: Optional[ProgressFn], timeout: float
) -> bool:
    """Stream the prebuilt full index (names, summaries, keywords) into the database at tmp.

    The file is decompressed and inserted row by row, so memory use stays flat no matter how
    big PyPI gets.  Returns False (and the caller falls back) if it can't be downloaded or
    doesn't look healthy.
    """
    if progress:
        progress("Downloading the pipseek search index...")
    try:
        opened = _open_index(session, timeout)
    except (requests.RequestException, OSError):
        return False
    if opened is None:
        return False
    raw, expected, close, recheck = opened
    digest = hashlib.sha256()  # always computed, so later runs can tell if the index changed
    compressed = _Meter(raw, MAX_INDEX_DOWNLOAD, digest)
    bundled = {normalize(n): (s, k) for n, s, k in bundled_records()}
    stats = {"rows": 0, "summaries": 0}

    def records() -> Iterator[Record]:
        gz = gzip.GzipFile(fileobj=io.BufferedReader(compressed), mode="rb")
        text = io.TextIOWrapper(
            io.BufferedReader(_Meter(gz, MAX_INDEX_TEXT)), encoding="utf-8", newline="\n"
        )
        for name, _, summary, keywords in parse_lines(text):
            stats["rows"] += 1
            if summary:
                stats["summaries"] += 1
            elif not keywords:
                # Fill gaps (projects the builder hasn't reached yet) from bundled summaries.
                summary, keywords = bundled.get(normalize(name), ("", ""))
            yield name, summary, keywords

    if progress:
        progress("Building the local search database (about 10 seconds)...")
    conn = _connect(tmp)
    try:
        _build(conn, records(), "full")
        while compressed.readinto(bytearray(1 << 16)):
            pass  # hash any trailing bytes the gzip reader didn't need
    except (
        requests.RequestException,
        OSError,
        EOFError,
        zlib.error,
        UnicodeDecodeError,
        IndexRejected,
        sqlite3.Error,
    ) as exc:
        if progress:
            progress(f"The downloaded index was unusable ({exc}); using the fallback.")
        return False
    finally:
        conn.close()
        close()

    actual = digest.hexdigest()
    if expected is not None and actual != expected:
        # The index and its checksum are published one after the other, so a download that
        # lands between the two sees a mismatch.  Read the checksum again before giving up.
        expected = recheck()
        if actual != expected:
            if progress:
                progress("The downloaded index didn't match its checksum; using the fallback.")
            return False
    # A partial or broken asset (too few rows, or most summaries missing) isn't trusted.
    rows, summaries = stats["rows"], stats["summaries"]
    if rows < MIN_INDEX_RECORDS or summaries < rows * MIN_SUMMARY_SHARE:
        return False
    # Remember which index this is, so the daily check can skip unchanged downloads.
    conn = _connect(tmp)
    try:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('sha256', ?)", (actual,))
        conn.commit()
    finally:
        conn.close()
    return True


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
    timeout: float = TOP_TIMEOUT,
) -> dict[str, int]:
    """30-day download counts for the ~15k most popular projects, keyed by normalized name.

    If the download fails, an older copy is used and the next attempt waits TOP_RETRY, so an
    offline run doesn't stall on this every time.
    """
    key = "top-packages"
    if not refresh:
        cached = cache.get(key, TOP_TTL)
        if cached:
            return cached
        if cache.get("top-packages-failed", TOP_RETRY):
            return (cache.load(key) or {}).get("data") or {}
    try:
        if progress:
            progress("Fetching download statistics...")
        resp = session.get(TOP_URL, timeout=min(timeout, TOP_TIMEOUT))
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        data = {normalize(r["project"]): int(r["download_count"]) for r in rows}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        cache.set("top-packages-failed", True)
        return (cache.load(key) or {}).get("data") or {}
    cache.set(key, data)
    return data
