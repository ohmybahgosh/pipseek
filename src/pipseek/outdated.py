"""`pipseek --outdated`: what's out of date or vulnerable in your Python environment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from rich import box
from rich.table import Table
from rich.text import Text

from pipseek.env import PythonEnv, format_command
from pipseek.pypi import FetchError, PackageInfo, PyPIClient
from pipseek.render import age_style, human_age, plain, vuln_label
from pipseek.vulns import SEVERITY_ORDER, max_severity, min_fix


@dataclass
class EnvCheck:
    """The result of checking an environment.

    ``packages`` are the rows to show.  ``unreachable`` lists installed packages whose PyPI
    details couldn't be fetched (network error), and ``unchecked`` lists packages whose
    vulnerabilities couldn't be looked up on OSV.dev.  Both mean the report is incomplete.
    """

    packages: list[PackageInfo] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    vulns_checked: bool = True

    @property
    def complete(self) -> bool:
        return not self.unreachable and not self.unchecked

    @property
    def vulnerable(self) -> list[PackageInfo]:
        return [p for p in self.packages if p.installed_vulns]


def check_environment(
    env: PythonEnv,
    client: PyPIClient,
    include_all: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    workers: int = 16,
    vulns: bool = True,
) -> EnvCheck:
    """Every installed package that has an update or a known vulnerability (or all of them)."""
    installed = env.installed
    if progress:
        where = "PyPI and OSV.dev" if vulns else "PyPI"
        progress(f"Checking {len(installed)} installed packages against {where}...")

    def check(name: str) -> tuple[str, Optional[PackageInfo]]:
        try:
            pkg = client.fetch(name)
        except FetchError:
            return "unreachable", None
        if pkg is None:  # not on PyPI (local or private package)
            return "missing", None
        pkg.installed_version = installed.get(name)
        if vulns:
            client.add_vulns(pkg)
        return "ok", pkg

    result = EnvCheck(vulns_checked=vulns)
    everything: list[PackageInfo] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, (status, pkg) in zip(sorted(installed), pool.map(check, sorted(installed))):
            if status == "unreachable":
                result.unreachable.append(name)
            elif pkg is not None:
                everything.append(pkg)
                if vulns and pkg.installed_vulns is None:
                    result.unchecked.append(pkg.name)

    if include_all:
        rows = everything
    else:
        rows = [
            p
            for p in everything
            if p.update_available or p.installed_vulns or (vulns and p.installed_vulns is None)
        ]
    rows.sort(
        key=lambda p: (
            -SEVERITY_ORDER.get(max_severity(p.installed_vulns or []) or "", 0),
            -len(p.installed_vulns or []),
            p.installed_vulns is not None,
            not p.update_available,
            p.name.lower(),
        )
    )
    result.packages = rows
    return result


def outdated_table(packages: list[PackageInfo], checked: bool = True) -> Table:
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
            if p.installed_vulns is not None:
                vulns = Text("none known", style="dim")
            elif checked:
                vulns = Text("couldn't check", style="yellow")
            else:
                vulns = Text("not checked", style="dim")
        table.add_row(
            plain(p.name),
            installed,
            Text(p.version, style="" if p.update_available else "dim"),
            Text(human_age(p.released), style=age_style(p.released)),
            vulns,
            plain(min_fix(p.installed_vulns or []) or ""),
            plain(p.summary or ""),
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
    return format_command([env.python, "-m", "pip", "install", "--upgrade", *names])


def outdated_dict(p: PackageInfo) -> dict:
    return {
        "name": p.name,
        "installed": p.installed_version,
        "latest": p.version,
        "update_available": p.update_available,
        "released": p.released,
        # None means OSV.dev couldn't be reached (or wasn't asked); [] means none are known.
        "vulnerabilities": (
            None if p.installed_vulns is None else [v.to_dict() for v in p.installed_vulns]
        ),
        "fixed_in": min_fix(p.installed_vulns or []),
    }
