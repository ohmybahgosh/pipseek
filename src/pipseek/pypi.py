"""PyPI JSON API client, GitHub stats, and local install detection."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from packaging.version import InvalidVersion, Version

from pipseek import __version__
from pipseek.cache import Cache

USER_AGENT = f"pipseek/{__version__} (+https://github.com/ohmybahgosh/pipseek)"
DETAILS_TTL = 3600
GITHUB_TTL = 6 * 3600

_EMPTY = {"", "none", "unknown", "n/a", "unknown author", "null"}
_GITHUB_RE = re.compile(r"^/([^/]+)/([^/#?]+)")
_REPO_KEYS = ("source", "source code", "repository", "github", "code", "homepage", "home")


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in _EMPTY else value


@dataclass
class PackageInfo:
    name: str
    version: str
    summary: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    requires_python: Optional[str] = None
    released: Optional[str] = None  # ISO date of the latest release
    homepage: Optional[str] = None
    repository: Optional[str] = None
    documentation: Optional[str] = None
    pypi_url: str = ""
    keywords: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    yanked: bool = False
    downloads_30d: Optional[int] = None
    github_stars: Optional[int] = None
    github_forks: Optional[int] = None
    github_archived: Optional[bool] = None
    installed_version: Optional[str] = None

    @property
    def install_command(self) -> str:
        return f"pip install {self.name}"

    @property
    def update_available(self) -> bool:
        if not self.installed_version:
            return False
        try:
            return Version(self.version) > Version(self.installed_version)
        except InvalidVersion:
            return self.version != self.installed_version

    def to_dict(self) -> dict:
        data = asdict(self)
        data["install_command"] = self.install_command
        data["update_available"] = self.update_available
        return data


def parse_package(data: dict) -> PackageInfo:
    """Build a PackageInfo from a /pypi/<name>/json payload."""
    info = data.get("info") or {}
    name = info.get("name") or ""
    urls = {k.strip().lower(): v for k, v in (info.get("project_urls") or {}).items() if v}

    homepage = _clean(urls.get("homepage") or urls.get("home") or info.get("home_page"))
    repository = None
    for key in _REPO_KEYS:
        candidate = _clean(urls.get(key))
        if candidate and any(h in candidate for h in ("github.com", "gitlab.com", "codeberg.org")):
            repository = candidate
            break
    if repository is None:
        repository = _clean(urls.get("source") or urls.get("source code") or urls.get("repository"))
    documentation = _clean(urls.get("documentation") or urls.get("docs") or info.get("docs_url"))

    author = _clean(info.get("author")) or _clean(info.get("maintainer"))
    if not author:
        email = _clean(info.get("author_email")) or _clean(info.get("maintainer_email"))
        if email:
            # "Jane Doe <jane@example.com>" -> "Jane Doe"
            author = re.sub(r"\s*<[^>]*>", "", email.split(",")[0]).strip() or None

    license_ = _clean(info.get("license_expression")) or _clean(info.get("license"))
    if license_ and (len(license_) > 60 or "\n" in license_):
        license_ = None  # Some packages paste the full license text here.
    if not license_:
        for c in info.get("classifiers") or []:
            if c.startswith("License :: OSI Approved :: "):
                license_ = c.rsplit(" :: ", 1)[-1]
                break

    released = None
    times = [f.get("upload_time_iso_8601") or f.get("upload_time") for f in data.get("urls") or []]
    times = [t for t in times if t]
    if times:
        released = min(times)[:10]

    keywords_raw = info.get("keywords") or ""
    sep = "," if "," in keywords_raw else None
    keywords = [k.strip() for k in keywords_raw.split(sep) if k.strip()][:12]

    deps = [d for d in (info.get("requires_dist") or []) if "extra ==" not in d]

    return PackageInfo(
        name=name,
        version=info.get("version") or "?",
        summary=_clean(info.get("summary")),
        author=author,
        license=license_,
        requires_python=_clean(info.get("requires_python")),
        released=released,
        homepage=homepage,
        repository=repository,
        documentation=documentation,
        pypi_url=info.get("package_url") or f"https://pypi.org/project/{name}/",
        keywords=keywords,
        dependencies=deps,
        extras=list(info.get("provides_extra") or []),
        yanked=bool(info.get("yanked")),
    )


def github_repo(url: Optional[str]) -> Optional[tuple[str, str]]:
    """Extract (owner, repo) from a GitHub URL, or None."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    m = _GITHUB_RE.match(parsed.path)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner in ("sponsors", "orgs", "features", "topics"):
        return None
    return owner, repo


class PyPIClient:
    def __init__(
        self,
        cache: Optional[Cache] = None,
        session: Optional[requests.Session] = None,
        github: bool = True,
        installed: Optional[Callable[[str], Optional[str]]] = None,
        timeout: float = 10,
        max_workers: int = 12,
    ) -> None:
        self.cache = cache or Cache()
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.github = github
        self.installed = installed
        self.timeout = timeout
        self.max_workers = max_workers
        self.github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self._github_rate_limited = False

    def package(self, name: str, downloads: Optional[int] = None) -> Optional[PackageInfo]:
        key = f"pkg/{name.lower()}"
        data = self.cache.get(key, DETAILS_TTL)
        if data is None:
            try:
                resp = self.session.get(f"https://pypi.org/pypi/{name}/json", timeout=self.timeout)
            except requests.RequestException:
                return None
            if resp.status_code != 200:
                return None
            try:
                full = resp.json()
            except ValueError:
                return None
            # The "releases" map can be megabytes; we never need it.
            data = {"info": full.get("info"), "urls": full.get("urls")}
            self.cache.set(key, data)

        pkg = parse_package(data)
        pkg.downloads_30d = downloads
        if self.installed:
            pkg.installed_version = self.installed(pkg.name)
        if self.github:
            self._add_github(pkg)
        return pkg

    def packages(
        self, names: list[str], downloads: Optional[dict[str, int]] = None
    ) -> list[PackageInfo]:
        """Fetch many packages concurrently, preserving input order and skipping failures."""
        downloads = downloads or {}
        if not names:
            return []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(names))) as pool:
            results = pool.map(lambda n: self.package(n, downloads.get(n)), names)
            return [r for r in results if r is not None]

    def _add_github(self, pkg: PackageInfo) -> None:
        repo = github_repo(pkg.repository) or github_repo(pkg.homepage)
        if not repo:
            return
        key = f"gh/{repo[0].lower()}/{repo[1].lower()}"
        stats = self.cache.get(key, GITHUB_TTL)
        if stats is None:
            if self._github_rate_limited:
                return
            headers = {"Accept": "application/vnd.github+json"}
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"
            try:
                resp = self.session.get(
                    f"https://api.github.com/repos/{repo[0]}/{repo[1]}",
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException:
                return
            if resp.status_code in (403, 429):
                self._github_rate_limited = True
                return
            if resp.status_code != 200:
                stats = {}
            else:
                body = resp.json()
                stats = {
                    "stars": body.get("stargazers_count"),
                    "forks": body.get("forks_count"),
                    "archived": body.get("archived"),
                }
            self.cache.set(key, stats)
        pkg.github_stars = stats.get("stars")
        pkg.github_forks = stats.get("forks")
        pkg.github_archived = stats.get("archived")


def days_since(iso_date: Optional[str]) -> Optional[int]:
    if not iso_date:
        return None
    try:
        then = datetime.strptime(iso_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).days
