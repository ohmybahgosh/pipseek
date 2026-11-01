"""Build the pipseek search index: every PyPI project with its summary and keywords.

PyPI has no public full-text search API, so pipseek ships its own index. This script is run
on a schedule by GitHub Actions (see .github/workflows/index.yml) and publishes the result as
a release asset that clients download.

It is incremental: the PyPI Simple API reports a "last serial" for every project, which
changes whenever the project changes. Only projects whose serial moved since the previous
build are re-fetched from the JSON API, so after the first build a refresh touches a few
thousand projects instead of ~1M. A time budget lets the first build span several runs.

Output format (gzipped TSV, one project per line):

    #pipseek-index v1 <unix-time>
    name <TAB> serial <TAB> summary <TAB> keywords

Usage:
    python -m pipseek.build_index --out pypi-index.tsv.gz [--previous old.tsv.gz]
    python -m pipseek.build_index --out popular.tsv.gz --popular-only
"""

from __future__ import annotations

import argparse
import gzip
import os
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from pipseek import __version__

SIMPLE_URL = "https://pypi.org/simple/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
TOP_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json"
)
HEADER = "#pipseek-index v1"
USER_AGENT = f"pipseek-index-builder/{__version__} (+https://github.com/ohmybahgosh/pipseek)"

Record = tuple[str, int, str, str]  # name, serial, summary, keywords


def clean(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())  # collapses tabs/newlines
    if text.lower() in ("unknown", "none", "n/a", "null"):
        return ""
    return text[:limit]


def read_index(path: str) -> Iterator[Record]:
    """Read a pipseek index file (gzipped or plain TSV)."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        yield from parse_lines(fh)


def parse_lines(lines: Iterable[str]) -> Iterator[Record]:
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            serial = int(parts[1])
        except ValueError:
            serial = 0
        summary = parts[2] if len(parts) > 2 else ""
        keywords = parts[3] if len(parts) > 3 else ""
        yield name, serial, summary, keywords


def write_index(path: str, records: Iterable[Record]) -> int:
    count = 0
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(f"{HEADER} {int(time.time())}\n")
        for name, serial, summary, keywords in records:
            fh.write(f"{name}\t{serial}\t{summary}\t{keywords}\n")
            count += 1
    os.replace(tmp, path)
    return count


def fetch_projects(session: requests.Session) -> dict[str, int]:
    resp = session.get(SIMPLE_URL, headers={"Accept": SIMPLE_ACCEPT}, timeout=120)
    resp.raise_for_status()
    return {p["name"]: int(p.get("_last-serial") or 0) for p in resp.json()["projects"]}


def fetch_popular(session: requests.Session) -> list[str]:
    resp = session.get(TOP_URL, timeout=60)
    resp.raise_for_status()
    return [row["project"] for row in resp.json()["rows"]]


def fetch_metadata(session: requests.Session, name: str) -> Optional[tuple[str, str]]:
    """Return (summary, keywords), ("", "") for projects without releases, None on error."""
    for attempt in range(4):
        try:
            resp = session.get(f"https://pypi.org/pypi/{name}/json", timeout=30)
        except requests.RequestException:
            time.sleep(2**attempt)
            continue
        if resp.status_code == 404:
            return "", ""
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2**attempt * 2)
            continue
        if resp.status_code != 200:
            return None
        try:
            info = resp.json().get("info") or {}
        except ValueError:
            return None
        keywords = info.get("keywords") or ""
        if isinstance(keywords, list):
            keywords = ", ".join(keywords)
        return clean(info.get("summary"), 300), clean(keywords, 200)
    return None


def build(
    out: str,
    previous: Optional[str] = None,
    popular_only: bool = False,
    time_budget: float = 5 * 3600,
    concurrency: int = 32,
    limit: Optional[int] = None,
) -> None:
    started = time.time()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    adapter = requests.adapters.HTTPAdapter(pool_connections=concurrency, pool_maxsize=concurrency)
    session.mount("https://", adapter)

    log("Fetching the PyPI project list...")
    serials = fetch_projects(session)
    log(f"{len(serials):,} projects on PyPI")

    old: dict[str, Record] = {}
    if previous:
        try:
            old = {r[0]: r for r in read_index(previous)}
            log(f"Loaded {len(old):,} records from {previous}")
        except OSError as exc:
            log(f"Could not read previous index ({exc}); starting fresh")

    if popular_only:
        by_norm = {n.lower().replace("_", "-").replace(".", "-"): n for n in serials}
        names = []
        for p in fetch_popular(session):
            real = by_norm.get(p.lower().replace("_", "-").replace(".", "-"))
            if real:
                names.append(real)
    else:
        names = list(serials)

    todo = [n for n in names if n not in old or old[n][1] != serials[n] or old[n][1] == 0]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo):,} projects need fetching")

    results: dict[str, Record] = {}
    lock = threading.Lock()
    done = 0
    out_of_time = threading.Event()

    def work(name: str) -> None:
        nonlocal done
        if out_of_time.is_set():
            return
        meta = fetch_metadata(session, name)
        with lock:
            if meta is not None:
                results[name] = (name, serials[name], meta[0], meta[1])
            done += 1
            if done % 5000 == 0:
                rate = done / max(time.time() - started, 1)
                log(f"  {done:,}/{len(todo):,} fetched ({rate:.0f}/s)")
        if time.time() - started > time_budget:
            out_of_time.set()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for fut in as_completed([pool.submit(work, n) for n in todo]):
            fut.result()

    if out_of_time.is_set():
        log("Time budget reached; remaining projects will be fetched on the next run.")

    def merged() -> Iterator[Record]:
        for name in names:
            if name in results:
                yield results[name]
            elif name in old:
                yield old[name]
            else:
                yield name, 0, "", ""  # serial 0 means "fetch me next time"

    count = write_index(out, merged())
    missing = sum(1 for n in names if n not in results and n not in old)
    elapsed = time.time() - started
    log(f"Wrote {count:,} records to {out} ({missing:,} pending) in {elapsed:.0f}s")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Build the pipseek search index.")
    p.add_argument("--out", required=True, help="output .tsv.gz path")
    p.add_argument("--previous", help="previous index to update incrementally")
    p.add_argument("--popular-only", action="store_true", help="only the ~15k most popular")
    p.add_argument("--time-budget", type=float, default=5 * 3600, help="seconds (default 5h)")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--limit", type=int, help="fetch at most N projects (for testing)")
    args = p.parse_args(argv)
    build(
        args.out,
        previous=args.previous,
        popular_only=args.popular_only,
        time_budget=args.time_budget,
        concurrency=args.concurrency,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
