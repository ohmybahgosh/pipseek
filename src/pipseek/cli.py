"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import Optional

from rich.console import Console
from rich.text import Text

from pipseek import __version__
from pipseek.cache import Cache
from pipseek.env import PythonEnv
from pipseek.index import IndexUnavailable, PackageIndex, load_downloads, normalize
from pipseek.pypi import FetchError, PyPIClient, offline_package
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
      pipseek --outdated            what's out of date or vulnerable in your environment
      pipseek --outdated --strict   same, but fail if anything installed is vulnerable (CI)

    --outdated --strict exit codes:
      0  nothing installed is known to be vulnerable
      1  something installed has a known vulnerability
      2  some lookups failed (PyPI or OSV.dev unreachable), so the check is incomplete

    interactive keys:
      /  search    enter  run search    up/down or j/k  move
      n/p  next/previous page    s  cycle sort    c  copy install command
      o  open on PyPI    h  open homepage/repo    i  pip install (or upgrade)    q  quit

    environment:
      PIPSEEK_CACHE_DIR         override the cache location
      PIPSEEK_INDEX_URL         use another prebuilt index: an http(s) URL, a file:// URL
                                or a local path to a pypi-index.tsv.gz file
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
    p.add_argument(
        "-o",
        "--outdated",
        action="store_true",
        help="list installed packages that are out of date or have known vulnerabilities",
    )
    p.add_argument(
        "--all", action="store_true", help="with --outdated: list every installed package"
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="with --outdated: exit 1 if an installed version has a known vulnerability, "
        "or 2 if some lookups failed",
    )
    p.add_argument("-P", "--plain", action="store_true", help="print results, no interactive UI")
    p.add_argument("-j", "--json", action="store_true", help="print results as JSON")
    p.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="results per page")
    p.add_argument("-p", "--page", type=int, default=1, metavar="N", help="page number")
    p.add_argument("-s", "--sort", choices=SORTS, default="relevance", help="result ordering")
    p.add_argument(
        "--python", metavar="PATH", help="interpreter used for installed checks and installs"
    )
    p.add_argument(
        "--no-stats",
        "--no-github",
        dest="no_stats",
        action="store_true",
        help="skip repository stats and vulnerability checks (also with --outdated)",
    )
    p.add_argument("--refresh", action="store_true", help="re-download the package index now")
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the search database and all cached data, then exit",
    )
    p.add_argument("-V", "--version", action="version", version=f"pipseek {__version__}")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.outdated:
        for flag in ("all", "strict"):
            if getattr(args, flag):
                parser.error(f"--{flag} only works with --outdated")
    if args.strict and args.no_stats:
        parser.error("--strict needs vulnerability checks, so it can't be used with --no-stats")
    err = Console(stderr=True)
    out = Console()
    cache = Cache()

    if args.clear_cache:
        removed = cache.clear()
        err.print(Text(f"Removed {removed} cached file(s) from {cache.root}"))
        return 0

    if args.limit < 1 or args.page < 1:
        err.print(Text("--limit and --page must be positive.", style="red"))
        return 2

    env = PythonEnv(args.python)
    client = PyPIClient(cache=cache, stats=not args.no_stats, installed=env.version_of)
    query = " ".join(args.query).strip()
    interactive = (
        not (args.plain or args.json or args.info or args.outdated) and sys.stdout.isatty()
    )

    def make_searcher(progress=None) -> Searcher:
        index = PackageIndex.load(
            session=client.session, cache=cache, refresh=args.refresh, progress=progress
        )
        return Searcher(index, client)

    try:
        if args.info:
            return _info(args, client, cache, out, err)

        if args.outdated:
            return _outdated(args, env, cache, out, err)

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
        err.print(Text(str(exc), style="red"))
        return 1
    except KeyboardInterrupt:
        return 130

    if args.json:
        print(json.dumps([p.to_dict() for p in page.packages], indent=2))
        return 0

    if not page.packages:
        err.print(Text.assemble("No packages match ", (query, "bold"), "."))
    else:
        out.print(results_table(page.packages, show_stars=not args.no_stats))
        footer = (
            f"{page.start}-{page.end} of {page.total:,} matches  |  page {page.page}/{page.pages}"
        )
        if page.has_next:
            footer += f"  |  next: --page {page.page + 1}"
        err.print(Text(footer, style="dim"))
        offline = sum(1 for p in page.packages if p.offline)
        if offline:
            err.print(
                Text(
                    f"Could not reach PyPI for {offline} of these, so they show only the name "
                    "and summary from the local index.",
                    style="yellow",
                )
            )
    if page.suggestions:
        err.print(Text.assemble(("Did you mean: ", "yellow"), ", ".join(page.suggestions)))
    return 0


def _outdated(args, env: PythonEnv, cache: Cache, out: Console, err: Console) -> int:
    from pipseek.outdated import check_environment, outdated_dict, outdated_table, upgrade_command

    client = PyPIClient(cache=cache, stats=False, installed=env.version_of)
    check_vulns = not args.no_stats
    if not env.installed:
        err.print(Text(f"Could not list the packages installed for {env.python}.", style="red"))
        return 1
    with err.status("Checking installed packages...") as status:
        result = check_environment(
            env,
            client,
            include_all=args.all,
            progress=lambda m: status.update(Text(m)),
            vulns=check_vulns,
        )
    packages = result.packages
    vulnerable = result.vulnerable
    if args.json:
        rows = [outdated_dict(p) for p in packages]
        rows += [
            {
                "name": name,
                "installed": env.installed.get(name),
                "latest": None,
                "update_available": None,
                "released": None,
                "vulnerabilities": None,
                "fixed_in": None,
                "error": "could not reach PyPI",
            }
            for name in result.unreachable
        ]
        print(json.dumps(rows, indent=2))
    else:
        err.print(Text(f"Environment: {env.python}", style="dim"))
        total = len(env.installed)
        if not packages and result.complete:
            tail = ", with no known vulnerabilities." if check_vulns else "."
            err.print(Text(f"All {total} installed packages are up to date{tail}", style="green"))
        elif packages:
            out.print(outdated_table(packages, checked=check_vulns))
            outdated = sum(1 for p in packages if p.update_available)
            summary = f"{outdated} outdated"
            if check_vulns:
                summary += f", {len(vulnerable)} with known vulnerabilities"
            summary += f", out of {total} installed."
            if check_vulns:
                summary += "  Vulnerability data: OSV.dev"
            err.print(Text(summary, style="dim"))
            if env.externally_managed:
                err.print(
                    Text.assemble(
                        (
                            "This is a system Python managed by your OS, so pip shouldn't "
                            "upgrade it directly.",
                            "yellow",
                        ),
                        "  Update these through your package manager, "
                        "or install what you need in a virtualenv or with pipx.",
                    )
                )
            else:
                cmd = upgrade_command(env, packages)
                if cmd:
                    label = "Upgrade: " if outdated <= 12 else "Start with: "
                    err.print(Text.assemble((label, "bold"), cmd))
        if not check_vulns:
            err.print(Text("Vulnerabilities were not checked (--no-stats).", style="dim"))
    _warn_incomplete(result, err)
    if args.strict:
        if vulnerable:
            return 1
        if not result.complete:
            return 2
    return 0


def _warn_incomplete(result, err: Console) -> None:
    def names(items: list[str]) -> str:
        shown = ", ".join(items[:8])
        return shown + (f" and {len(items) - 8} more" if len(items) > 8 else "")

    if result.unreachable:
        err.print(
            Text(
                f"Could not reach PyPI for {len(result.unreachable)} package(s), so they were "
                f"not checked: {names(result.unreachable)}",
                style="yellow",
            )
        )
    if result.unchecked:
        err.print(
            Text(
                f"Could not reach OSV.dev for {len(result.unchecked)} package(s), so their "
                f"vulnerabilities are unknown: {names(result.unchecked)}",
                style="yellow",
            )
        )


def _info(args, client: PyPIClient, cache: Cache, out: Console, err: Console) -> int:
    name = args.info
    downloads = load_downloads(client.session, cache).get(normalize(name))
    try:
        pkg = client.fetch(name, downloads)
    except FetchError as exc:
        index = PackageIndex.open_cached(cache)
        match = index.get(name) if index else None
        if match is None:
            err.print(Text(f"Could not reach PyPI to look up '{name}' ({exc}).", style="red"))
            return 1
        pkg = offline_package(match.name, match.summary, match.downloads)
        err.print(
            Text("Could not reach PyPI, so this shows what the local index has.", style="yellow")
        )
    if pkg is None:
        err.print(Text(f"Package '{name}' was not found on PyPI.", style="red"))
        return 1
    if args.json:
        print(json.dumps(pkg.to_dict(), indent=2))
    else:
        out.print(package_panel(pkg, full=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
