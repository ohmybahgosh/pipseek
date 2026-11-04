"""Rich renderers shared by the plain CLI output and the TUI."""

from __future__ import annotations

from typing import Optional

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pipseek.pypi import PackageInfo, days_since
from pipseek.vulns import max_severity, min_fix


def plain(value) -> Text:
    """Text that Rich shows literally.

    Summaries, authors, URLs and queries come from PyPI uploaders and users, so square brackets
    in them must never be read as Rich markup (``[link=...]``, ``[/]``).
    """
    if isinstance(value, Text):
        return value
    return Text("" if value is None else str(value))


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


SEVERITY_STYLE = {
    "CRITICAL": "bold white on red",
    "HIGH": "bold red",
    "MODERATE": "yellow",
    "MEDIUM": "yellow",
    "LOW": "cyan",
}


def vuln_label(vulns: Optional[list]) -> Optional[Text]:
    """Short '2 vulns (HIGH)' text, or None when unknown/none."""
    if not vulns:
        return None
    worst = max_severity(vulns)
    word = "vuln" if len(vulns) == 1 else "vulns"
    text = Text(f"{len(vulns)} {word}", style="bold red")
    if worst:
        text.append(f" ({worst.lower()})", style="red")
    return text


def vulns_text(vulns: list, limit: int = 5) -> Table:
    """A compact list: severity, CVE (or advisory id), fix version, and the summary under it."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column()
    for v in vulns[:limit]:
        line = Text(v.cve or v.id, style="bold")
        if v.fixed:
            line.append(f"  fixed in {v.fixed}", style="green")
        if v.summary:
            line.append("\n" + v.summary, style="dim")
        sev = (v.severity or "unknown").lower()
        grid.add_row(Text(sev, style=SEVERITY_STYLE.get(v.severity or "", "dim")), line)
    if len(vulns) > limit:
        grid.add_row("", Text(f"...and {len(vulns) - limit} more", style="dim"))
    return grid


def status_badges(pkg: PackageInfo) -> Text:
    text = Text()
    if pkg.offline:
        text.append(" PyPI unreachable: name and summary from the local index ", "black on yellow")
        text.append(" ")
    if pkg.installed_vulns:
        n = len(pkg.installed_vulns)
        fix = min_fix(pkg.installed_vulns)
        word = "vulnerability" if n == 1 else "vulnerabilities"
        msg = f" {n} known {word} in your installed {pkg.installed_version}"
        msg += f", fixed in {fix} " if fix else " "
        text.append(msg, "bold white on red")
        text.append(" ")
    elif pkg.installed_version:
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
        if pkg.vulns or pkg.installed_vulns:
            name.append(" !", style="bold red")
        row = [
            name,
            plain("-" if pkg.offline else pkg.version or "-"),
            Text(human_age(pkg.released), style=age_style(pkg.released)),
            human_count(pkg.downloads_30d),
        ]
        if show_stars:
            row.append(human_count(pkg.stars))
        no_summary = Text("no description", style="dim italic")
        row.append(plain(pkg.summary) if pkg.summary else no_summary)
        table.add_row(*row)
    return table


def package_panel(pkg: PackageInfo, full: bool = False) -> Panel:
    """A detailed card for one package."""
    version = f"v{pkg.version}" if pkg.version and pkg.version != "?" else ""
    title = Text.assemble((pkg.name, "bold white"), "  ", (version, "bold green"))

    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold cyan", no_wrap=True)
    body.add_column()

    def row(label: str, value) -> None:
        if value:
            body.add_row(plain(label), value if not isinstance(value, str) else plain(value))

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
    if pkg.vulns is not None:
        latest = f"in {pkg.version} (latest), from OSV.dev"
        if pkg.vulns:
            row(f"Vulns ({len(pkg.vulns)})", vulns_text(pkg.vulns))
        else:
            row("Vulns", Text.assemble(("none known", "green"), (f"  {latest}", "dim")))
    if pkg.installed_vulns and pkg.installed_version != pkg.version:
        label = f"In {pkg.installed_version} ({len(pkg.installed_vulns)})"
        row(label, vulns_text(pkg.installed_vulns, limit=3))
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
