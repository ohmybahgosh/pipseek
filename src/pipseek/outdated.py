"""`pipseek --outdated`: what's out of date or vulnerable in your Python environment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from rich import box
from rich.table import Table
from rich.text import Text

from pipseek.env import PythonEnv
from pipseek.pypi import PackageInfo, PyPIClient
from pipseek.render import age_style, human_age, vuln_label
from pipseek.vulns import SEVERITY_ORDER, max_severity, min_fix


def check_environment(
    env: PythonEnv,
    client: PyPIClient,
    include_all: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    workers: int = 16,
) -> list[PackageInfo]:
    """Every installed package that has an update or a known vulnerability (or all of them)."""
    installed = env.installed
    if progress:
        progress(f"Checking {len(installed)} installed packages against PyPI and OSV.dev...")

    def check(name: str) -> Optional[PackageInfo]:
        pkg = client.package(name)
        if pkg is None:  # not on PyPI (local or private package)
            return None
        pkg.installed_version = installed.get(name)
        client.add_vulns(pkg)
        return pkg

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [p for p in pool.map(check, sorted(installed)) if p is not None]

    if not include_all:
        results = [p for p in results if p.update_available or p.installed_vulns]
    results.sort(
        key=lambda p: (
            -SEVERITY_ORDER.get(max_severity(p.installed_vulns or []) or "", 0),
            -len(p.installed_vulns or []),
            not p.update_available,
            p.name.lower(),
        )
    )
    return results


def outdated_table(packages: list[PackageInfo]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold cyan")
    table.add_column("Package", style="bold", no_wrap=True)
    table.add_column("Installed", no_wrap=True)
    table.add_column("Latest", style="green", no_wrap=True)
    table.add_column("Released", no_wrap=True, justify="right")
    table.add_column("Vulnerabilities", no_wrap=True)
    table.add_column("Fixed in", no_wrap=True, style="green")
    table.add_column("Summary", ratio=1, overflow="ellipsis", no_wrap=True, min_width=12)
    for p in packages:
        installed = Text(p.installed_version or "-", style="yellow" if p.update_available else "")
        vulns = vuln_label(p.installed_vulns)
        if vulns is None:
            vulns = Text("none known" if p.installed_vulns is not None else "?", style="dim")
        table.add_row(
            p.name,
            installed,
            p.version if p.update_available else Text(p.version, style="dim"),
            Text(human_age(p.released), style=age_style(p.released)),
            vulns,
            min_fix(p.installed_vulns or []) or "",
            p.summary or "",
        )
    return table


def upgrade_command(env: PythonEnv, packages: list[PackageInfo], limit: int = 12) -> Optional[str]:
    """A pip command for the most important upgrades: vulnerable packages first."""
    names = [p.name for p in packages if p.update_available]
    if not names:
        return None
    vulnerable = [p.name for p in packages if p.update_available and p.installed_vulns]
    if len(names) > limit and vulnerable:
        names = vulnerable
    names = names[:limit]
    return " ".join([env.python, "-m", "pip", "install", "--upgrade", *names])


def outdated_dict(p: PackageInfo) -> dict:
    return {
        "name": p.name,
        "installed": p.installed_version,
        "latest": p.version,
        "update_available": p.update_available,
        "released": p.released,
        "vulnerabilities": [v.to_dict() for v in p.installed_vulns or []],
        "fixed_in": min_fix(p.installed_vulns or []),
    }
