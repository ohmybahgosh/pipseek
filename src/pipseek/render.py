"""Rich renderers shared by the plain CLI output and the TUI."""

from __future__ import annotations

from typing import Optional

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pipseek.pypi import PackageInfo, days_since


def human_count(n: Optional[int]) -> str:
    if n is None:
        return "-"
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            value = n / size
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    return str(n)


def human_age(iso_date: Optional[str]) -> str:
    days = days_since(iso_date)
    if days is None:
        return "-"
    if days < 1:
        return "today"
    if days < 31:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    years = days / 365
    return f"{years:.1f}y ago" if years < 10 else f"{int(years)}y ago"


def age_style(iso_date: Optional[str]) -> str:
    days = days_since(iso_date)
    if days is None:
        return "dim"
    if days < 180:
        return "green"
    if days < 730:
        return "yellow"
    return "red"


def scorecard_style(score: Optional[float]) -> str:
    if score is None:
        return "dim"
    if score >= 7:
        return "green"
    if score >= 4:
        return "yellow"
    return "red"


def status_badges(pkg: PackageInfo) -> Text:
    text = Text()
    if pkg.installed_version:
        if pkg.update_available:
            text.append(f" installed {pkg.installed_version}, update available ", "black on yellow")
        else:
            text.append(" installed ", "black on green")
        text.append(" ")
    if pkg.yanked:
        text.append(" yanked ", "white on red")
    return text


def results_table(packages: list[PackageInfo], show_stars: bool = True) -> Table:
    show_stars = show_stars and any(p.stars is not None for p in packages)
    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold cyan")
    table.add_column("Package", style="bold", no_wrap=True)
    table.add_column("Version", style="green", no_wrap=True)
    table.add_column("Released", no_wrap=True, justify="right")
    table.add_column("DL/30d", no_wrap=True, justify="right")
    if show_stars:
        table.add_column("Stars", no_wrap=True, justify="right")
    table.add_column("Summary", ratio=1, overflow="ellipsis", no_wrap=True, min_width=20)

    for pkg in packages:
        name = Text(pkg.name)
        if pkg.installed_version:
            name.append(" *", style="yellow" if pkg.update_available else "green")
        row = [
            name,
            pkg.version,
            Text(human_age(pkg.released), style=age_style(pkg.released)),
            human_count(pkg.downloads_30d),
        ]
        if show_stars:
            row.append(human_count(pkg.stars))
        row.append(pkg.summary or Text("no description", style="dim italic"))
        table.add_row(*row)
    return table


def package_panel(pkg: PackageInfo, full: bool = False) -> Panel:
    """A detailed card for one package."""
    title = Text.assemble((pkg.name, "bold white"), "  ", (f"v{pkg.version}", "bold green"))

    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold cyan", no_wrap=True)
    body.add_column()

    def row(label: str, value) -> None:
        if value:
            body.add_row(label, value)

    badges = status_badges(pkg)
    if badges:
        body.add_row("", badges)
    row("Summary", pkg.summary)
    body.add_row("Install", Text(pkg.install_command, style="bold bright_green"))
    row(
        "Released",
        Text.assemble(
            (pkg.released or "-", ""), "  ", (human_age(pkg.released), age_style(pkg.released))
        ),
    )
    if pkg.downloads_30d:
        row("Downloads", f"{pkg.downloads_30d:,} in the last 30 days")
    if pkg.stars is not None:
        host = (pkg.source_project or "").split("/", 1)[0]
        site = {"github.com": "GitHub", "gitlab.com": "GitLab", "bitbucket.org": "Bitbucket"}.get(
            host, "Repo"
        )
        stats = f"{pkg.stars:,} stars, {pkg.forks or 0:,} forks"
        if pkg.open_issues is not None:
            stats += f", {pkg.open_issues:,} open issues"
        row(site, stats)
    if pkg.scorecard is not None:
        row(
            "Security",
            Text.assemble(
                (f"{pkg.scorecard:.1f}/10", scorecard_style(pkg.scorecard)),
                ("  OpenSSF Scorecard", "dim"),
            ),
        )
    row("Author", pkg.author)
    row("License", pkg.license)
    row("Python", pkg.requires_python)
    row("Homepage", pkg.homepage)
    if pkg.repository and pkg.repository != pkg.homepage:
        row("Source", pkg.repository)
    row("Docs", pkg.documentation)
    row("PyPI", pkg.pypi_url)
    if full:
        if pkg.keywords:
            row("Keywords", ", ".join(pkg.keywords))
        if pkg.extras:
            row("Extras", ", ".join(pkg.extras))
        if pkg.dependencies:
            deps = Text("\n".join(pkg.dependencies), style="dim")
            row(f"Requires ({len(pkg.dependencies)})", deps)
        else:
            row("Requires", Text("no dependencies", style="dim"))

    return Panel(Group(body), title=title, title_align="left", box=box.ROUNDED, border_style="cyan")
