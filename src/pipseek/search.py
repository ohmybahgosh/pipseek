"""High-level search: rank names locally, then hydrate one page of results from PyPI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pipseek.index import Match, PackageIndex, normalize
from pipseek.pypi import PackageInfo, PyPIClient, offline_package

SORTS = ("relevance", "downloads", "updated", "name", "stars")


@dataclass
class SearchPage:
    query: str
    page: int
    per_page: int
    total: int
    packages: list[PackageInfo]
    suggestions: list[str] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def start(self) -> int:
        return (self.page - 1) * self.per_page + 1 if self.total else 0

    @property
    def end(self) -> int:
        return min(self.page * self.per_page, self.total)


class Searcher:
    def __init__(self, index: PackageIndex, client: PyPIClient) -> None:
        self.index = index
        self.client = client
        self._last: tuple[str, list[Match]] = ("", [])

    def matches(self, query: str) -> list[Match]:
        if self._last[0] != query:
            self._last = (query, self.index.search(query))
        return self._last[1]

    def search(
        self, query: str, page: int = 1, per_page: int = 20, sort: str = "relevance"
    ) -> SearchPage:
        if sort not in SORTS:
            raise ValueError(f"Unknown sort {sort!r}; choose from {', '.join(SORTS)}")
        matches = list(self.matches(query))

        # Sorts that only need index data apply to the full result set.
        if sort == "downloads":
            matches.sort(key=lambda m: (-(m.downloads or 0), -m.score))
        elif sort == "name":
            matches.sort(key=lambda m: m.name.lower())

        total = len(matches)
        page = max(1, page)
        chunk = matches[(page - 1) * per_page : page * per_page]
        names = [m.name for m in chunk]
        by_name = {m.name: m for m in chunk}

        def from_index(name: str) -> PackageInfo:
            m = by_name[name]
            return offline_package(m.name, m.summary, m.downloads)

        packages = self.client.packages(
            names, {m.name: m.downloads for m in chunk}, fallback=from_index
        )

        # Sorts that need PyPI/GitHub data apply within the fetched page.
        if sort == "updated":
            packages.sort(key=lambda p: p.released or "", reverse=True)
        elif sort == "stars":
            packages.sort(key=lambda p: p.stars or -1, reverse=True)

        suggestions: list[str] = []
        single_word = len(query.split()) == 1
        exact = any(normalize(m.name) == normalize(query) for m in matches[:10])
        if page == 1 and single_word and not exact:
            top = {normalize(m.name) for m in matches[:10]}
            suggestions = [
                s
                for s in self.index.suggest(query)
                if normalize(s) != normalize(query) and normalize(s) not in top
            ][:3]

        return SearchPage(query, page, per_page, total, packages, suggestions)

    def info(self, name: str) -> Optional[PackageInfo]:
        downloads = self.index.downloads.get(normalize(name)) if self.index else None
        return self.client.package(name, downloads)
