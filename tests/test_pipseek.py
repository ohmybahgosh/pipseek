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

    def packages(self, names, downloads=None):
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
