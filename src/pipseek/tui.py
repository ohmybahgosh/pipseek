"""Interactive Textual interface."""

from __future__ import annotations

import webbrowser
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static
from textual.worker import get_current_worker

from pipseek import __version__
from pipseek.env import PythonEnv, format_command
from pipseek.index import IndexUnavailable
from pipseek.pypi import PackageInfo
from pipseek.render import age_style, human_age, human_count, package_panel, vuln_label
from pipseek.search import SORTS, Searcher, SearchPage


class ResultItem(ListItem):
    def __init__(self, pkg: PackageInfo) -> None:
        super().__init__()
        self.pkg = pkg

    def compose(self) -> ComposeResult:
        pkg = self.pkg
        line = Text()
        line.append(pkg.name, style="bold")
        if pkg.offline:
            line.append("  offline", style="yellow")
        else:
            line.append(f"  {pkg.version}", style="green")
        if pkg.installed_version:
            line.append("  *", style="yellow" if pkg.update_available else "green")
        meta = Text()
        meta.append(human_age(pkg.released), style=age_style(pkg.released))
        if pkg.downloads_30d:
            meta.append(f"  {human_count(pkg.downloads_30d)} dl/30d", style="cyan")
        if pkg.stars is not None:
            meta.append(f"  {human_count(pkg.stars)} stars", style="magenta")
        label = vuln_label(pkg.installed_vulns or pkg.vulns)
        if label:
            meta.append("  ")
            meta.append(label)
        summary = Text(pkg.summary or "no description", style="dim", overflow="ellipsis")
        summary.truncate(200)
        yield Label(line)
        yield Label(meta)
        yield Label(summary, classes="summary")


class PipSeekApp(App):
    TITLE = "pipseek"
    SUB_TITLE = f"PyPI search v{__version__}"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #query { margin: 0 1; }
    #body { height: 1fr; }
    #results { width: 45%; min-width: 30; border: round $primary-darken-2; }
    #results:focus { border: round $accent; }
    #results > ResultItem { padding: 0 1; height: auto; margin-bottom: 1; }
    #results > ResultItem .summary { width: 100%; height: 1; }
    #detail-wrap { width: 1fr; border: round $primary-darken-2; }
    #status { height: 1; padding: 0 2; color: $text-muted; }
    """

    BINDINGS = [
        Binding("/", "focus_search", "Search"),
        Binding("escape", "focus_results", "Results", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("n", "next_page", "Next"),
        Binding("p", "prev_page", "Prev"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("c", "copy_install", "Copy"),
        Binding("o", "open_pypi", "PyPI"),
        Binding("h", "open_home", "Home"),
        Binding("i", "install", "Install"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        searcher_factory,
        query: str = "",
        sort: str = "relevance",
        per_page: int = 20,
        env: Optional[PythonEnv] = None,
    ) -> None:
        super().__init__()
        self._factory = searcher_factory
        self.searcher: Optional[Searcher] = None
        self.query_text = query
        self.sort = sort
        self.per_page = per_page
        self.page_num = 1
        self.current: Optional[SearchPage] = None
        self.env = env

    # Layout --------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(value=self.query_text, placeholder="Search PyPI (press / to focus)", id="query")
        with Horizontal(id="body"):
            yield ListView(id="results")
            with VerticalScroll(id="detail-wrap"):
                yield Static(id="detail")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.set_status("Loading package index...")
        self.load_index()

    # Helpers -------------------------------------------------------------------------------

    def set_status(self, message) -> None:
        """Show a message under the results.  Plain strings are shown literally, never as markup,
        because they can contain the user's query or text from PyPI."""
        if not isinstance(message, Text):
            message = Text(str(message))
        self.query_one("#status", Static).update(message)

    @property
    def selected(self) -> Optional[PackageInfo]:
        item = self.query_one("#results", ListView).highlighted_child
        return item.pkg if isinstance(item, ResultItem) else None

    def show_detail(self, pkg: Optional[PackageInfo]) -> None:
        detail = self.query_one("#detail", Static)
        detail.update(package_panel(pkg, full=True) if pkg else "")

    # Workers -------------------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="index")
    def load_index(self) -> None:
        try:
            self.searcher = self._factory(lambda msg: self.call_from_thread(self.set_status, msg))
        except IndexUnavailable as exc:
            self.call_from_thread(self.set_status, Text(str(exc), style="red"))
            return
        index = self.searcher.index
        note = "" if index.source == "full" else " (descriptions for popular packages only)"
        self.call_from_thread(self.set_status, f"Index ready: {len(index):,} packages{note}.")
        if self.query_text:
            self.call_from_thread(self.run_search)
        else:
            self.call_from_thread(self.action_focus_search)

    def run_search(self) -> None:
        if not self.searcher:
            self.set_status("Still loading the index...")
            return
        if not self.query_text.strip():
            return
        self.set_status(f"Searching for '{self.query_text}' (page {self.page_num}, {self.sort})...")
        self._search_worker(self.query_text, self.page_num, self.sort)

    @work(thread=True, exclusive=True, group="search")
    def _search_worker(self, query: str, page: int, sort: str) -> None:
        assert self.searcher is not None
        result = self.searcher.search(query, page=page, per_page=self.per_page, sort=sort)
        if get_current_worker().is_cancelled:
            return
        self.call_from_thread(self._show_page, result)

    async def _show_page(self, result: SearchPage) -> None:
        self.current = result
        lv = self.query_one("#results", ListView)
        await lv.clear()
        await lv.extend(ResultItem(p) for p in result.packages)
        if result.packages:
            lv.index = 0
            lv.focus()
            self.show_detail(result.packages[0])
        else:
            self.show_detail(None)

        if result.total:
            msg = (
                f"{result.start}-{result.end} of {result.total:,} matches for "
                f"'{result.query}'  |  page {result.page}/{result.pages}  |  sort: {self.sort}"
            )
        else:
            msg = f"No packages match '{result.query}'."
        if result.suggestions:
            msg += f"  |  did you mean: {', '.join(result.suggestions)}?"
        if any(p.offline for p in result.packages):
            msg += "  |  PyPI unreachable: showing the local index"
        self.set_status(msg)

    # Events --------------------------------------------------------------------------------

    @on(Input.Submitted, "#query")
    def _submitted(self, event: Input.Submitted) -> None:
        self.query_text = event.value.strip()
        self.page_num = 1
        self.run_search()

    @on(ListView.Highlighted, "#results")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, ResultItem):
            self.show_detail(event.item.pkg)

    # Actions -------------------------------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#query", Input).focus()

    def action_focus_results(self) -> None:
        self.query_one("#results", ListView).focus()

    def action_cursor_down(self) -> None:
        self.query_one("#results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#results", ListView).action_cursor_up()

    def action_next_page(self) -> None:
        if self.current and self.current.has_next:
            self.page_num += 1
            self.run_search()

    def action_prev_page(self) -> None:
        if self.page_num > 1:
            self.page_num -= 1
            self.run_search()

    def action_cycle_sort(self) -> None:
        self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        self.page_num = 1
        self.run_search()

    def action_copy_install(self) -> None:
        pkg = self.selected
        if pkg:
            self.copy_to_clipboard(pkg.install_command)
            self.notify(f"Copied: {pkg.install_command}", markup=False)

    def action_open_pypi(self) -> None:
        pkg = self.selected
        if pkg:
            webbrowser.open(pkg.pypi_url)

    def action_open_home(self) -> None:
        pkg = self.selected
        url = pkg and (pkg.repository or pkg.homepage or pkg.documentation)
        if url:
            webbrowser.open(url)
        elif pkg:
            self.notify("No homepage listed for this package.", severity="warning")

    def action_install(self) -> None:
        pkg = self.selected
        if not pkg or not self.env:
            return
        if self.env.externally_managed:
            self.notify(
                f"{self.env.python} is managed by your OS, so pip can't install into it. "
                "Activate a virtualenv or start pipseek with --python.",
                severity="warning",
                timeout=8,
                markup=False,
            )
            return
        upgrade = pkg.update_available
        cmd = format_command(self.env.install_command(pkg.name, upgrade))
        with self.suspend():
            print(f"\n$ {cmd}\n")
            code = self.env.install(pkg.name, upgrade)
            input(f"\npip exited with code {code}. Press Enter to return to pipseek...")
        if code == 0:
            pkg.installed_version = pkg.version
            self.show_detail(pkg)
            self.notify(f"Installed {pkg.name} {pkg.version}", markup=False)
        else:
            self.notify(
                f"pip install {pkg.name} failed (exit {code})", severity="error", markup=False
            )
