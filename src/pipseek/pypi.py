"""PyPI JSON API client, project stats from deps.dev, and vulnerabilities from OSV."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote, urlparse

import requests
from packaging.version import InvalidVersion, Version

from pipseek import __version__
from pipseek.cache import Cache
from pipseek.vulns import OSVClient, Vuln

USER_AGENT = f"pipseek/{__version__} (+https://github.com/ohmybahgosh/pipseek)"
DETAILS_TTL = 3600
STATS_TTL = 12 * 3600
DEPS_DEV_URL = "https://api.deps.dev/v3/projects/{}"

_EMPTY = {"", "none", "unknown", "n/a", "unknown author", "null"}
_REPO_PATH_RE = re.compile(r"^/([^/]+)/([^/#?]+)")
_SOURCE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
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
    source_project: Optional[str] = None  # e.g. github.com/psf/requests
    stars: Optional[int] = None
    forks: Optional[int] = None
    open_issues: Optional[int] = None
    scorecard: Optional[float] = None  # OpenSSF Scorecard, 0-10
    vulns: Optional[list[Vuln]] = None  # known vulnerabilities in the latest version
    installed_vulns: Optional[list[Vuln]] = None  # ...and in the installed version
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


def source_project(url: Optional[str]) -> Optional[str]:
    """GitHub/GitLab/Bitbucket URL -> deps.dev project key, e.g. github.com/psf/requests."""
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in _SOURCE_HOSTS:
        return None
    m = _REPO_PATH_RE.match(parsed.path)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner.lower() in ("sponsors", "orgs", "features", "topics", "users"):
        return None
    return f"{host}/{owner}/{repo}".lower()


class PyPIClient:
    def __init__(
        self,
        cache: Optional[Cache] = None,
        session: Optional[requests.Session] = None,
        stats: bool = True,
        installed: Optional[Callable[[str], Optional[str]]] = None,
        timeout: float = 10,
        max_workers: int = 12,
    ) -> None:
        self.cache = cache or Cache()
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.stats = stats
        self.osv = OSVClient(self.session, self.cache, timeout)
        self.installed = installed
        self.timeout = timeout
        self.max_workers = max_workers

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
        if self.stats:
            self._add_project_stats(pkg)
            self.add_vulns(pkg)
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

    def add_vulns(self, pkg: PackageInfo) -> None:
        """Known vulnerabilities (OSV.dev) for the latest and the installed version."""
        if pkg.version and pkg.version != "?":
            pkg.vulns = self.osv.query(pkg.name, pkg.version)
        if pkg.installed_version:
            if pkg.installed_version == pkg.version:
                pkg.installed_vulns = pkg.vulns
            else:
                pkg.installed_vulns = self.osv.query(pkg.name, pkg.installed_version)

    def _add_project_stats(self, pkg: PackageInfo) -> None:
        """Stars, forks, issues and OpenSSF Scorecard from deps.dev (Google; free, no key)."""
        project = source_project(pkg.repository) or source_project(pkg.homepage)
        if not project:
            return
        pkg.source_project = project
        key = f"proj/{project}"
        stats = self.cache.get(key, STATS_TTL)
        if stats is None:
            try:
                resp = self.session.get(
                    DEPS_DEV_URL.format(quote(project, safe="")), timeout=self.timeout
                )
            except requests.RequestException:
                return
            if resp.status_code == 404:
                stats = {}
            elif resp.status_code != 200:
                return  # transient; try again next time
            else:
                try:
                    body = resp.json()
                except ValueError:
                    return
                score = (body.get("scorecard") or {}).get("overallScore")
                stats = {
                    "stars": body.get("starsCount"),
                    "forks": body.get("forksCount"),
                    "issues": body.get("openIssuesCount"),
                    "scorecard": score,
                }
            self.cache.set(key, stats)
        pkg.stars = stats.get("stars")
        pkg.forks = stats.get("forks")
        pkg.open_issues = stats.get("issues")
        pkg.scorecard = stats.get("scorecard")


def days_since(iso_date: Optional[str]) -> Optional[int]:
    if not iso_date:
        return None
    try:
        then = datetime.strptime(iso_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).days
