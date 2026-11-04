import json

import pytest

from pipseek import cli
from pipseek.cache import Cache
from pipseek.index import PackageIndex, normalize, tokens
from pipseek.pypi import PackageInfo, parse_package, source_project
from pipseek.search import Searcher

RECORDS = [
    ("requests", "Python HTTP for Humans.", "http, client"),
    ("requests-oauthlib", "OAuthlib authentication support for Requests.", ""),
    ("requests-toolbelt", "A utility belt for advanced users of python-requests", ""),
    ("grequests", "Requests + Gevent", ""),
    ("my-requests-thing-with-a-very-long-name", "", ""),
    ("httpx", "The next generation HTTP client.", ""),
    ("Flask", "A simple framework for building complex web applications.", ""),
    ("flask-login", "User session management for Flask", ""),
    ("pdf-parser", "", ""),
    ("pypdf", "A pure-python PDF library capable of splitting and merging pages", "pdf"),
    ("tqdm", "Fast, Extensible Progress Meter", "progressbar, progressmeter"),
]
NAMES = [r[0] for r in RECORDS]
DOWNLOADS = {"requests": 1_000_000_000, "requests-oauthlib": 200_000_000, "flask": 100_000_000}


@pytest.fixture
def index():
    return PackageIndex.from_records(RECORDS, DOWNLOADS)


class FakeClient:
    def package(self, name, downloads=None):
        return PackageInfo(name=name, version="1.0", downloads_30d=downloads)

    def packages(self, names, downloads=None, fallback=None):
        downloads = downloads or {}
        return [self.package(n, downloads.get(n)) for n in names]


def test_normalize_and_tokens():
    assert normalize("Foo_Bar.baz") == "foo-bar-baz"
    assert tokens("web framework!") == ["web", "framework"]


def test_exact_match_ranks_first(index):
    names = [m.name for m in index.search("requests")]
    assert names[0] == "requests"
    assert "grequests" in names
    assert names.index("requests-oauthlib") < names.index("my-requests-thing-with-a-very-long-name")


def test_case_and_separator_insensitive(index):
    assert index.search("FLASK")[0].name == "Flask"
    assert index.search("pdf_parser")[0].name == "pdf-parser"


def test_multi_word_query(index):
    assert index.search("flask login")[0].name == "flask-login"


def test_description_search(index):
    assert index.search("web framework")[0].name == "Flask"
    assert index.search("progress meter")[0].name == "tqdm"
    assert "tqdm" in [m.name for m in index.search("progressbar")]
    assert index.search("http client")[0].name in ("httpx", "requests")
    pdf = [m.name for m in index.search("pdf")]
    assert pdf[:2] == ["pdf-parser", "pypdf"] or set(pdf[:2]) == {"pdf-parser", "pypdf"}


def test_bundled_popular_summaries():
    from pipseek.index import bundled_records

    records = {name.lower(): summary for name, summary, _ in bundled_records()}
    assert len(records) > 10_000
    assert records["requests"]


def test_build_index_roundtrip(tmp_path):
    from pipseek.build_index import read_index, write_index

    path = str(tmp_path / "idx.tsv.gz")
    write_index(path, [("a", 1, "alpha pkg", "x, y"), ("b", 0, "", "")])
    assert list(read_index(path)) == [("a", 1, "alpha pkg", "x, y"), ("b", 0, "", "")]


def test_no_match(index):
    assert index.search("zzzzzz") == []
    assert index.search("   ") == []


def test_suggest_typo(index):
    assert "requests" in index.suggest("reqeusts")


def test_search_pagination_and_sort(index):
    s = Searcher(index, FakeClient())
    page1 = s.search("requests", page=1, per_page=2)
    assert page1.total >= 5
    assert page1.pages >= 3
    assert page1.has_next
    assert [p.name for p in page1.packages] == ["requests", "requests-oauthlib"]
    last = s.search("requests", page=page1.pages, per_page=2)
    assert not last.has_next
    assert last.end == page1.total

    by_name = s.search("requests", per_page=10, sort="name")
    assert [p.name for p in by_name.packages] == sorted(p.name for p in by_name.packages)

    with pytest.raises(ValueError):
        s.search("requests", sort="bogus")


def test_parse_package_cleans_fields():
    pkg = parse_package(
        {
            "info": {
                "name": "demo",
                "version": "2.0.0",
                "summary": "  A demo  ",
                "author": "",
                "author_email": "Jane Doe <jane@example.com>",
                "license": "UNKNOWN",
                "classifiers": ["License :: OSI Approved :: MIT License"],
                "project_urls": {
                    "Homepage": "https://demo.dev",
                    "Source": "https://github.com/jane/demo.git",
                    "Documentation": "https://docs.demo.dev",
                },
                "requires_dist": ["rich>=13", "pytest; extra == 'test'"],
                "provides_extra": ["test"],
                "keywords": "cli, demo",
                "requires_python": ">=3.9",
            },
            "urls": [
                {"upload_time_iso_8601": "2025-02-01T10:00:00Z"},
                {"upload_time_iso_8601": "2025-01-31T09:00:00Z"},
            ],
        }
    )
    assert pkg.summary == "A demo"
    assert pkg.author == "Jane Doe"
    assert pkg.license == "MIT License"
    assert pkg.homepage == "https://demo.dev"
    assert pkg.repository == "https://github.com/jane/demo.git"
    assert pkg.documentation == "https://docs.demo.dev"
    assert pkg.dependencies == ["rich>=13"]
    assert pkg.extras == ["test"]
    assert pkg.keywords == ["cli", "demo"]
    assert pkg.released == "2025-01-31"
    assert pkg.pypi_url == "https://pypi.org/project/demo/"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/psf/requests", "github.com/psf/requests"),
        ("https://www.github.com/Jane/Demo.git", "github.com/jane/demo"),
        ("https://github.com/jane/tag.git-tools", "github.com/jane/tag.git-tools"),
        ("https://gitlab.com/group/proj", "gitlab.com/group/proj"),
        ("https://github.com/sponsors/jane", None),
        ("https://example.com/a/b", None),
        (None, None),
    ],
)
def test_source_project(url, expected):
    assert source_project(url) == expected


def test_update_available():
    pkg = PackageInfo(name="x", version="2.0.0", installed_version="1.9.9")
    assert pkg.update_available
    pkg.installed_version = "2.0.0"
    assert not pkg.update_available
    assert pkg.to_dict()["install_command"] == "pip install x"


def test_cache_roundtrip(tmp_path):
    cache = Cache(tmp_path)
    cache.set("k", {"a": 1}, etag="e")
    assert cache.get("k", 60) == {"a": 1}
    assert cache.get("k", 0) is None
    assert cache.load("k")["etag"] == "e"
    assert cache.clear() == 1


def test_cli_json(monkeypatch, tmp_path, capsys, index):
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.PackageIndex, "load", classmethod(lambda cls, **kw: index))
    monkeypatch.setattr(cli, "PyPIClient", lambda **kw: _FakeClientWithSession())
    assert cli.main(["flask", "--json", "-n", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["name"] for d in data] == ["Flask", "flask-login"]
    assert data[0]["install_command"] == "pip install Flask"


class _FakeClientWithSession(FakeClient):
    session = None


def test_cli_plain(monkeypatch, tmp_path, capsys, index):
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.PackageIndex, "load", classmethod(lambda cls, **kw: index))
    monkeypatch.setattr(cli, "PyPIClient", lambda **kw: _FakeClientWithSession())
    assert cli.main(["flask", "--plain", "-n", "2"]) == 0
    assert "flask-login" in capsys.readouterr().out
    assert cli.main(["flask", "--plain", "--no-stats"]) == 0
    assert cli.main(["flask", "--plain", "--no-github"]) == 0


def test_package_panel_renders_stats():
    from rich.console import Console

    from pipseek.render import package_panel

    pkg = PackageInfo(name="x", version="1.0", source_project="gitlab.com/a/x", stars=1200, forks=3)
    pkg.scorecard = 7.5
    console = Console(record=True, width=100)
    console.print(package_panel(pkg, full=True))
    text = console.export_text()
    assert "GitLab" in text and "1,200 stars" in text and "7.5/10" in text


# Vulnerabilities and --outdated ------------------------------------------------------------

OSV_RECORDS = [
    {
        "id": "PYSEC-2024-1",
        "aliases": ["GHSA-aaaa-bbbb-cccc", "CVE-2024-0001"],
        "details": "Same bug, PyPA copy",
        "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.32.0"}]}]}],
    },
    {
        "id": "GHSA-aaaa-bbbb-cccc",
        "aliases": ["CVE-2024-0001", "PYSEC-2024-1"],
        "summary": "Session ignores verify=False",
        "database_specific": {"severity": "MODERATE"},
        "affected": [
            {
                "package": {"ecosystem": "PyPI", "name": "requests"},
                "ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.32.0"}]}],
            }
        ],
    },
    {
        "id": "GHSA-dddd-eeee-ffff",
        "aliases": ["CVE-2024-0002"],
        "summary": "Credential leak",
        "database_specific": {"severity": "HIGH"},
        "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.32.4"}]}]}],
    },
]


def test_osv_records_merge_aliases_and_sort_by_severity():
    from pipseek.vulns import max_severity, min_fix, parse_records

    vulns = parse_records(OSV_RECORDS, "2.25.0")
    assert [v.id for v in vulns] == ["GHSA-dddd-eeee-ffff", "GHSA-aaaa-bbbb-cccc"]
    assert vulns[0].cve == "CVE-2024-0002"
    assert vulns[1].severity == "MODERATE"
    assert vulns[1].fixed == "2.32.0"
    assert max_severity(vulns) == "HIGH"
    assert min_fix(vulns) == "2.32.4"  # the version that fixes both


def test_fixed_version_ignores_fixes_below_current():
    from pipseek.vulns import parse_records

    rec = {
        "id": "GHSA-x",
        "affected": [
            {"ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.0"}]}]},
            {"ranges": [{"events": [{"introduced": "2.0"}, {"fixed": "2.5"}]}]},
        ],
    }
    assert parse_records([rec], "2.1")[0].fixed == "2.5"


def test_panel_shows_installed_vulnerabilities():
    from rich.console import Console

    from pipseek.render import package_panel
    from pipseek.vulns import parse_records

    pkg = PackageInfo(name="requests", version="2.32.5", installed_version="2.25.0")
    pkg.vulns = []
    pkg.installed_vulns = parse_records(OSV_RECORDS, "2.25.0")
    console = Console(record=True, width=120)
    console.print(package_panel(pkg, full=True))
    text = console.export_text()
    assert "2 known vulnerabilities in your installed 2.25.0, fixed in 2.32.4" in text
    assert "none known" in text and "CVE-2024-0002" in text


class _FakeEnv:
    python = "/usr/bin/python3"
    installed = {"requests": "2.25.0", "rich": "15.0.0", "mylocal": "0.1"}

    def version_of(self, name):
        return self.installed.get(normalize(name))


class _FakePyPI:
    latest = {"requests": "2.32.5", "rich": "15.0.0"}

    def fetch(self, name, downloads=None):
        if name not in self.latest:
            return None
        return PackageInfo(name=name, version=self.latest[name])

    def add_vulns(self, pkg):
        from pipseek.vulns import parse_records

        bad = pkg.name == "requests"
        pkg.installed_vulns = parse_records(OSV_RECORDS, pkg.installed_version) if bad else []


def test_check_environment_lists_outdated_and_vulnerable():
    from pipseek.outdated import check_environment, outdated_dict, upgrade_command

    env, client = _FakeEnv(), _FakePyPI()
    result = check_environment(env, client)
    rows = result.packages
    assert result.complete
    assert [p.name for p in rows] == ["requests"]
    assert outdated_dict(rows[0])["fixed_in"] == "2.32.4"
    assert upgrade_command(env, rows) == "/usr/bin/python3 -m pip install --upgrade requests"
    assert [p.name for p in check_environment(env, client, include_all=True).packages] == [
        "requests",
        "rich",
    ]


def test_fixed_version_prefers_final_releases():
    from pipseek.vulns import parse_records

    rec = {
        "id": "GHSA-y",
        "affected": [
            {"ranges": [{"events": [{"introduced": "0"}, {"fixed": "26.10.0rc1"}]}]},
            {"ranges": [{"events": [{"introduced": "0"}, {"fixed": "26.10.0"}]}]},
        ],
    }
    assert parse_records([rec], "24.11.0")[0].fixed == "26.10.0"


def test_upgrade_command_prioritises_vulnerable_packages():
    from pipseek.outdated import upgrade_command

    pkgs = [PackageInfo(name=f"p{i}", version="2", installed_version="1") for i in range(20)]
    pkgs[5].installed_vulns = [object()]
    assert upgrade_command(_FakeEnv(), pkgs).endswith("--upgrade p5")
    assert len(upgrade_command(_FakeEnv(), pkgs[:3]).split()) == 8
