"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import Optional

from rich.console import Console

from pipseek import __version__
from pipseek.cache import Cache
from pipseek.env import PythonEnv
from pipseek.index import IndexUnavailable, PackageIndex, load_downloads, normalize
from pipseek.pypi import PyPIClient
from pipseek.render import package_panel, results_table
from pipseek.search import SORTS, Searcher

EPILOG = textwrap.dedent(
    """\
    examples:
      pipseek                       open the interactive search
      pipseek fastapi               search and browse results
      pipseek "pdf parser" -P       print a table instead of opening the TUI
      pipseek http -s downloads -n 10 --json | jq '.[].name'
      pipseek --info requests       full details for one package

    interactive keys:
      /  search    enter  run search    up/down or j/k  move
      n/p  next/previous page    s  cycle sort    c  copy install command
      o  open on PyPI    h  open homepage/repo    i  pip install (or upgrade)    q  quit

    environment:
      GITHUB_TOKEN / GH_TOKEN   raise the GitHub API rate limit for star counts
      PIPSEEK_CACHE_DIR         override the cache location
    """
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipseek",
        description="Search PyPI from your terminal.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", nargs="*", help="search terms")
    p.add_argument("-i", "--info", metavar="PACKAGE", help="show full details for one package")
    p.add_argument("-P", "--plain", action="store_true", help="print results, no interactive UI")
    p.add_argument("-j", "--json", action="store_true", help="print results as JSON")
    p.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="results per page")
    p.add_argument("-p", "--page", type=int, default=1, metavar="N", help="page number")
    p.add_argument("-s", "--sort", choices=SORTS, default="relevance", help="result ordering")
    p.add_argument(
        "--python", metavar="PATH", help="interpreter used for installed checks and installs"
    )
    p.add_argument("--no-github", action="store_true", help="skip GitHub star lookups")
    p.add_argument("--refresh", action="store_true", help="re-download the package index now")
    p.add_argument("--clear-cache", action="store_true", help="delete cached data and exit")
    p.add_argument("-V", "--version", action="version", version=f"pipseek {__version__}")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    err = Console(stderr=True)
    out = Console()
    cache = Cache()

    if args.clear_cache:
        removed = cache.clear()
        err.print(f"Removed {removed} cached file(s) from {cache.root}")
        return 0

    if args.limit < 1 or args.page < 1:
        err.print("[red]--limit and --page must be positive.[/]")
        return 2

    env = PythonEnv(args.python)
    client = PyPIClient(cache=cache, github=not args.no_github, installed=env.version_of)
    query = " ".join(args.query).strip()
    interactive = not (args.plain or args.json or args.info) and sys.stdout.isatty()

    def make_searcher(progress=None) -> Searcher:
        index = PackageIndex.load(
            session=client.session, cache=cache, refresh=args.refresh, progress=progress
        )
        return Searcher(index, client)

    try:
        if args.info:
            return _info(args, client, cache, out, err)

        if interactive:
            from pipseek.tui import PipSeekApp

            PipSeekApp(make_searcher, query, args.sort, args.limit, env).run()
            return 0

        if not query:
            build_parser().print_help()
            return 2

        with err.status("Loading package index...") as status:
            searcher = make_searcher(lambda msg: status.update(msg))
            status.update(f"Searching {len(searcher.index):,} packages...")
            page = searcher.search(query, page=args.page, per_page=args.limit, sort=args.sort)
    except IndexUnavailable as exc:
        err.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        return 130

    if args.json:
        print(json.dumps([p.to_dict() for p in page.packages], indent=2))
        return 0

    if not page.packages:
        err.print(f"No packages match [bold]{query}[/].")
    else:
        out.print(results_table(page.packages, show_github=not args.no_github))
        footer = (
            f"{page.start}-{page.end} of {page.total:,} matches  |  page {page.page}/{page.pages}"
        )
        if page.has_next:
            footer += f"  |  next: --page {page.page + 1}"
        err.print(f"[dim]{footer}[/]")
    if page.suggestions:
        err.print(f"[yellow]Did you mean:[/] {', '.join(page.suggestions)}")
    return 0


def _info(args, client: PyPIClient, cache: Cache, out: Console, err: Console) -> int:
    downloads = load_downloads(client.session, cache).get(normalize(args.info))
    pkg = client.package(args.info, downloads)
    if pkg is None:
        err.print(f"[red]Package '{args.info}' was not found on PyPI.[/]")
        return 1
    if args.json:
        print(json.dumps(pkg.to_dict(), indent=2))
    else:
        out.print(package_panel(pkg, full=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
