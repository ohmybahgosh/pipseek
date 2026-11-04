"""Tests for the fixes from the v2.1.0 review: untrusted text, threads, offline use, caches,
the index download, Windows paths and the --outdated exit codes."""

import gzip
import hashlib
import json
import sys
import threading
import time

import pytest
import requests
from rich.console import Console

from pipseek import cli
from pipseek import index as index_mod
from pipseek.cache import Cache
from pipseek.env import PythonEnv, _venv_python, find_python, format_command
from pipseek.index import PackageIndex, tokens
from pipseek.pypi import FetchError, PackageInfo, PyPIClient
from pipseek.render import package_panel
from pipseek.search import Searcher, SearchPage

NASTY = [
    "qiniu://<bucket>[/name_of_dir]",
    "use agentassert-abc[claude-code] instead",
    "[link=https://evil.example]click me[/link] [bold red]FAKE BADGE[/]",
    "foo [/]",
]


def render(renderable, width=200) -> str:
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()


# 1. Rich markup injection -----------------------------------------------------------------


@pytest.mark.parametrize("text", NASTY)
def test_remote_text_is_shown_literally(text):
    from pipseek.outdated import outdated_table
    from pipseek.render import package_panel, results_table

    pkg = PackageInfo(
        name="evil",
        version="1.0",
        summary=text,
        author=text,
        license="MIT",
        homepage=f"https://example.com/{text}",
        keywords=[text],
        extras=[text],
        installed_version="0.9",
    )
    assert text in render(results_table([pkg]))
    out = render(package_panel(pkg, full=True), width=400)
    assert out.count(text) >= 4  # summary, author, keywords, extras
    assert text in render(outdated_table([pkg]))


def test_rendered_markup_creates_no_links_or_styles():
    from pipseek.render import package_panel

    pkg = PackageInfo(name="evil", version="1.0", summary=NASTY[2], author=NASTY[2])
    console = Console(width=200)
    segments = list(console.render(package_panel(pkg)))
    assert not [seg for seg in segments if seg.style and seg.style.link]
    fake = [seg for seg in segments if "FAKE BADGE" in seg.text]
    assert fake and all(not (seg.style and seg.style.color) for seg in fake)


@pytest.mark.parametrize("query", ["foo [/]", "[link=x]y", "x [bold]"])
def test_cli_query_with_markup(monkeypatch, tmp_path, capsys, query):
    idx = PackageIndex.from_records([("requests", "Python HTTP for Humans.", "")])
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.PackageIndex, "load", classmethod(lambda cls, **kw: idx))
    monkeypatch.setattr(cli, "PyPIClient", lambda **kw: _OfflineFake())
    assert cli.main([query, "--plain"]) == 0
    assert f"No packages match {query}." in capsys.readouterr().err


def test_cli_plain_with_markup_in_summary(monkeypatch, tmp_path, capsys):
    idx = PackageIndex.from_records([("lektor-qiniu", NASTY[0], "")])
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.PackageIndex, "load", classmethod(lambda cls, **kw: idx))

    class Client(_OfflineFake):
        def packages(self, names, downloads=None, fallback=None):
            return [PackageInfo(name=n, version="0.1", summary=NASTY[0]) for n in names]

    monkeypatch.setattr(cli, "PyPIClient", lambda **kw: Client())
    assert cli.main(["lektor-qiniu", "--plain"]) == 0
    assert "[/name_of_dir]" in capsys.readouterr().out


def test_tui_status_line_shows_query_literally():
    import asyncio

    from pipseek.tui import PipSeekApp

    idx = PackageIndex.from_records([("requests", "Python HTTP for Humans.", "")])

    async def go():
        app = PipSeekApp(lambda progress: Searcher(idx, _OfflineFake()))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            query = "foo [/] [link=x]y"
            await app._show_page(SearchPage(query, 1, 20, 0, []))
            await pilot.pause()
            status = app.query_one("#status")
            assert query in str(status.render())

    asyncio.run(go())


class _OfflineFake:
    session = None

    def packages(self, names, downloads=None, fallback=None):
        return []


# 2. The installed list and threads ----------------------------------------------------------


def test_installed_is_complete_for_every_thread(monkeypatch):
    env = PythonEnv("python3")
    calls = []

    def slow_list():
        calls.append(1)
        time.sleep(0.2)
        return {"requests": "2.32.0", "rich": "14.0.0"}

    monkeypatch.setattr(env, "_list_installed", slow_list)
    seen = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        seen.append((env.version_of("requests"), env.version_of("Rich")))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [("2.32.0", "14.0.0")] * 16
    assert len(calls) == 1


# 3. --outdated --strict when lookups fail ---------------------------------------------------


class _Env:
    python = "/usr/bin/python3"
    externally_managed = False

    def __init__(self, installed):
        self.installed = installed

    def version_of(self, name):
        return self.installed.get(name)


class _Client:
    """latest: name -> version; vulns: name -> list|None; offline: names PyPI can't reach."""

    def __init__(self, latest, vulns=None, offline=()):
        self.latest, self.vulns, self.offline = latest, vulns or {}, set(offline)
        self.session = None
        self.vuln_calls = 0

    def fetch(self, name, downloads=None):
        if name in self.offline:
            raise FetchError("offline")
        if name not in self.latest:
            return None
        return PackageInfo(name=name, version=self.latest[name])

    def add_vulns(self, pkg):
        self.vuln_calls += 1
        pkg.installed_vulns = self.vulns.get(pkg.name, [])


def _run_outdated(monkeypatch, tmp_path, env, client, *flags):
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "PythonEnv", lambda python=None: env)
    monkeypatch.setattr(cli, "PyPIClient", lambda **kw: client)
    return cli.main(["--outdated", *flags])


def test_strict_exits_2_when_osv_unreachable(monkeypatch, tmp_path, capsys):
    env = _Env({"requests": "2.25.0", "rich": "14.0.0"})
    client = _Client({"requests": "2.25.0", "rich": "14.0.0"}, vulns={"requests": None})
    assert _run_outdated(monkeypatch, tmp_path, env, client, "--strict", "--json") == 2
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["requests"]["vulnerabilities"] is None  # unknown, not "none"
    assert "rich" not in rows


def test_strict_exits_2_when_pypi_unreachable(monkeypatch, tmp_path, capsys):
    env = _Env({"requests": "2.25.0"})
    client = _Client({}, offline={"requests"})
    assert _run_outdated(monkeypatch, tmp_path, env, client, "--strict") == 2
    err = capsys.readouterr().err
    assert "up to date" not in err
    assert "Could not reach PyPI for 1 package" in err


def test_strict_exits_1_when_vulnerable(monkeypatch, tmp_path):
    from pipseek.vulns import Vuln

    env = _Env({"requests": "2.25.0", "rich": "14.0.0"})
    client = _Client(
        {"requests": "2.32.0", "rich": "14.0.0"},
        vulns={"requests": [Vuln("GHSA-1", severity="HIGH")], "rich": None},
    )
    assert _run_outdated(monkeypatch, tmp_path, env, client, "--strict") == 1


def test_strict_exits_0_when_clean(monkeypatch, tmp_path, capsys):
    env = _Env({"rich": "14.0.0"})
    client = _Client({"rich": "14.0.0"})
    assert _run_outdated(monkeypatch, tmp_path, env, client, "--strict") == 0
    assert "up to date, with no known vulnerabilities" in capsys.readouterr().err


def test_outdated_no_stats_skips_osv(monkeypatch, tmp_path, capsys):
    env = _Env({"requests": "2.25.0"})
    client = _Client({"requests": "2.32.0"})
    assert _run_outdated(monkeypatch, tmp_path, env, client, "--no-stats", "--json") == 0
    assert client.vuln_calls == 0
    assert json.loads(capsys.readouterr().out)[0]["vulnerabilities"] is None


# 4. Offline search and --info ---------------------------------------------------------------


class _DownSession:
    """A requests session with no network: every request fails."""

    headers: dict = {}

    def get(self, url, **kw):
        raise requests.ConnectionError("network is down")

    post = get


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


RECORDS = [
    ("requests", "Python HTTP for Humans.", ""),
    ("requests-toolbelt", "A utility belt for advanced users of python-requests", ""),
    ("grequests", "Requests + Gevent", ""),
]


def test_fetch_tells_not_found_from_unreachable(tmp_path):
    client = PyPIClient(cache=Cache(tmp_path), stats=False)
    client.session.get = lambda url, **kw: _Resp(404)
    assert client.fetch("nope") is None
    client.session.get = lambda url, **kw: _Resp(503)
    with pytest.raises(FetchError):
        client.fetch("nope")
    assert client.package("nope") is None  # the old, lenient API


def test_fetch_uses_stale_cache_when_offline(tmp_path):
    cache = Cache(tmp_path)
    cache.set("pkg/requests", {"info": {"name": "requests", "version": "2.0"}, "urls": []})
    entry = cache.load("pkg/requests")
    entry["ts"] = 0  # very old
    with gzip.open(cache._path("pkg/requests"), "wt") as fh:
        json.dump(entry, fh)
    client = PyPIClient(cache=cache, session=_DownSession(), stats=False)
    pkg = client.fetch("requests")
    assert pkg.version == "2.0" and not pkg.offline


def test_offline_search_falls_back_to_index(tmp_path):
    idx = PackageIndex.from_records(RECORDS)
    client = PyPIClient(cache=Cache(tmp_path), session=_DownSession(), stats=False)
    page = Searcher(idx, client).search("requests", per_page=2)
    assert page.total == 3
    assert len(page.packages) == 2  # the page isn't shortened
    assert page.packages[0].name == "requests"
    assert page.packages[0].offline
    assert page.packages[0].summary == "Python HTTP for Humans."
    assert "PyPI unreachable" in render(package_panel(page.packages[0]))


def test_info_offline_uses_local_index(monkeypatch, tmp_path, capsys):
    idx = PackageIndex.from_records(RECORDS)
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.PackageIndex, "open_cached", classmethod(lambda cls, cache: idx))
    real = cli.PyPIClient
    monkeypatch.setattr(cli, "PyPIClient", lambda **kw: real(session=_DownSession(), **kw))
    assert cli.main(["--info", "requests", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["summary"] == "Python HTTP for Humans."
    assert "Could not reach PyPI" in captured.err
    assert cli.main(["--info", "not-in-index"]) == 1
    assert "Could not reach PyPI" in capsys.readouterr().err


def test_info_not_found_online(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    real = cli.PyPIClient

    def client(**kw):
        c = real(**kw)
        c.session.get = lambda url, **k: _Resp(404)
        return c

    monkeypatch.setattr(cli, "PyPIClient", client)
    assert cli.main(["--info", "surely-missing"]) == 1
    assert "was not found on PyPI" in capsys.readouterr().err


# 5 and 6. The cache -------------------------------------------------------------------------


def test_corrupt_cache_files_are_dropped(tmp_path):
    cache = Cache(tmp_path)
    cache.set("a", {"x": 1})
    path = cache._path("a")
    path.write_bytes(path.read_bytes()[:10])  # truncated: EOFError
    assert cache.load("a") is None
    assert not path.exists()
    path.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"garbage!")  # zlib.error
    assert cache.get("a", 60) is None
    assert not path.exists()
    path.write_bytes(b"not gzip at all")
    assert cache.load("a") is None


def test_clear_cache_removes_database_and_temp_files(tmp_path):
    cache = Cache(tmp_path)
    cache.set("a", {"x": 1})
    for name in ("index.sqlite", "index-abc.tmp", "a.json.gz.123.tmp"):
        (tmp_path / name).write_bytes(b"x")
    assert cache.clear() == 4
    assert list(tmp_path.iterdir()) == []


def test_cache_set_survives_failed_replace(tmp_path, monkeypatch):
    import pipseek.cache as cache_mod

    def boom(src, dst):
        raise PermissionError("in use")

    cache = Cache(tmp_path)
    monkeypatch.setattr(cache_mod.os, "replace", boom)
    cache.set("a", {"x": 1})  # no exception
    assert list(tmp_path.glob("*.tmp")) == []


# 7 and 8. Downloading and swapping in the index ---------------------------------------------


def _write_index(path, rows, summaries=True):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("#pipseek-index v1 0\n")
        for i in range(rows):
            summary = f"summary {i}" if summaries else ""
            fh.write(f"pkg{i}\t{i}\t{summary}\t\n")


class _SimpleOnly:
    """Serves the PyPI Simple API for the fallback; everything else is offline."""

    headers: dict = {}

    def get(self, url, **kw):
        if url == index_mod.SIMPLE_URL:
            return _Resp(200, {"projects": [{"name": "fallback-only"}]})
        raise requests.ConnectionError("offline")


@pytest.fixture
def small_index(monkeypatch):
    monkeypatch.setattr(index_mod, "MIN_INDEX_RECORDS", 10)
    monkeypatch.setattr(index_mod, "bundled_records", lambda: [])


def _load(tmp_path):
    cache = Cache(tmp_path / "cache")
    return PackageIndex.load(session=_SimpleOnly(), cache=cache, refresh=True)


def test_index_from_local_path_and_file_url(tmp_path, monkeypatch, small_index):
    src = tmp_path / "idx.tsv.gz"
    _write_index(src, 50)
    monkeypatch.setenv("PIPSEEK_INDEX_URL", str(src))
    idx = _load(tmp_path)
    assert idx.source == "full" and len(idx) == 50
    monkeypatch.setenv("PIPSEEK_INDEX_URL", src.as_uri())
    assert _load(tmp_path).source == "full"


def test_index_is_streamed_not_listed(tmp_path, monkeypatch, small_index):
    src = tmp_path / "idx.tsv.gz"
    _write_index(src, 20)
    monkeypatch.setenv("PIPSEEK_INDEX_URL", str(src))
    kinds = []
    real_build = index_mod._build

    def spy(conn, records, source):
        kinds.append(type(records).__name__)
        return real_build(conn, records, source)

    monkeypatch.setattr(index_mod, "_build", spy)
    _load(tmp_path)
    assert kinds == ["generator"]


@pytest.mark.parametrize(
    "case", ["bad-checksum", "good-checksum", "too-big", "few-summaries", "corrupt"]
)
def test_index_checks(tmp_path, monkeypatch, small_index, case):
    src = tmp_path / "idx.tsv.gz"
    _write_index(src, 50, summaries=case != "few-summaries")
    if case == "corrupt":
        src.write_bytes(src.read_bytes()[:-30])
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    if case == "bad-checksum":
        (tmp_path / "idx.tsv.gz.sha256").write_text("0" * 64 + "  pypi-index.tsv.gz\n")
    if case == "good-checksum":
        (tmp_path / "idx.tsv.gz.sha256").write_text(digest + "  pypi-index.tsv.gz\n")
    if case == "too-big":
        monkeypatch.setattr(index_mod, "MAX_INDEX_TEXT", 200)
    monkeypatch.setenv("PIPSEEK_INDEX_URL", str(src))
    idx = _load(tmp_path)
    if case == "good-checksum":
        assert idx.source == "full"
    else:
        assert idx.source == "names"  # rejected; fell back to the Simple API names
        assert idx.get("fallback-only") is not None
    assert list((tmp_path / "cache").glob("*.tmp")) == []


def test_failed_swap_keeps_old_database(tmp_path, monkeypatch, small_index):
    src = tmp_path / "idx.tsv.gz"
    _write_index(src, 30)
    monkeypatch.setenv("PIPSEEK_INDEX_URL", str(src))
    assert len(_load(tmp_path)) == 30
    _write_index(src, 40)

    def locked(src_path, dst_path):
        raise PermissionError("another pipseek has it open")

    monkeypatch.setattr(index_mod.os, "replace", locked)
    idx = _load(tmp_path)
    assert len(idx) == 30  # the old one, still usable
    assert list((tmp_path / "cache").glob("*.tmp")) == []


# 9. Windows and conda -----------------------------------------------------------------------


def test_conda_on_windows_python_at_env_root(tmp_path):
    (tmp_path / "python.exe").write_text("")
    assert _venv_python(str(tmp_path)) == str(tmp_path / "python.exe")


def test_store_stub_is_skipped_on_windows(monkeypatch):
    stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe"
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: stub)
    assert find_python() == sys.executable


def test_commands_with_spaces_are_quoted(monkeypatch):
    import shlex
    import subprocess

    from pipseek.outdated import upgrade_command

    cmd = ["/home/me/My Envs/bin/python", "-m", "pip", "install", "x"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert shlex.split(format_command(cmd)) == cmd
    monkeypatch.setattr(sys, "platform", "win32")
    assert format_command(cmd) == subprocess.list2cmdline(cmd)
    monkeypatch.setattr(sys, "platform", "linux")
    env = _Env({})
    env.python = "/home/me/My Envs/bin/python"
    pkgs = [PackageInfo(name="x", version="2", installed_version="1")]
    assert shlex.split(upgrade_command(env, pkgs))[0] == env.python


# 10. Small ones -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv", [["x", "--all"], ["x", "--strict"], ["--outdated", "--strict", "--no-stats"]]
)
def test_flag_combinations_are_checked(argv, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PIPSEEK_CACHE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "--" in capsys.readouterr().err


def test_non_ascii_queries():
    assert tokens("Ελληνικά café") == ["ελληνικά", "café"]
    idx = PackageIndex.from_records(
        [("greek-nlp", "Εργαλεία για ελληνικά κείμενα", ""), ("other", "Something else", "")]
    )
    assert [m.name for m in idx.search("ελληνικά")] == ["greek-nlp"]


def test_download_stats_back_off_after_failure(tmp_path):
    calls = []

    class Session:
        def get(self, url, **kw):
            calls.append(kw.get("timeout"))
            raise requests.ConnectionError("offline")

    cache = Cache(tmp_path)
    assert index_mod.load_downloads(Session(), cache) == {}
    assert index_mod.load_downloads(Session(), cache) == {}
    assert len(calls) == 1
    assert calls[0] <= index_mod.TOP_TIMEOUT


def test_index_check_before_publishing(tmp_path):
    from pipseek.build_index import check_index, main

    good = tmp_path / "good.tsv.gz"
    _write_index(good, 20)
    assert check_index(str(good), min_records=10) == []
    bad = tmp_path / "bad.tsv.gz"
    _write_index(bad, 20, summaries=False)
    assert "summary" in check_index(str(bad), min_records=10)[0]
    assert main(["--check", str(good)]) == 1  # fewer than the real 100,000 minimum
    assert list(tmp_path.glob("*.tmp")) == []


class _HttpIndex:
    """Serves an index over fake HTTP: a few 404s first, and checksums that change over time."""

    headers: dict = {}

    def __init__(self, data: bytes, missing: int = 0, checksums=()):
        self.data, self.missing, self.checksums = data, missing, list(checksums)
        self.index_calls = 0

    def get(self, url, **kw):

        if url.endswith(".sha256"):
            if not self.checksums:
                return _Text(404, "")
            value = self.checksums.pop(0) if len(self.checksums) > 1 else self.checksums[0]
            return _Text(200, f"{value}  pypi-index.tsv.gz\n")
        if url == index_mod.SIMPLE_URL:
            return _Resp(200, {"projects": [{"name": "fallback-only"}]})
        if url == index_mod.TOP_URL:
            raise requests.ConnectionError("no download stats in this test")
        self.index_calls += 1
        if self.index_calls <= self.missing:
            return _Stream(404, b"")
        return _Stream(200, self.data)


class _Text:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


class _Stream:
    def __init__(self, status, data):
        import io

        self.status_code = status
        self.headers = {"Content-Length": str(len(data))}
        self.raw = io.BytesIO(data)

    def close(self):
        pass


def _http_load(tmp_path, session):
    cache = Cache(tmp_path / "cache")
    return PackageIndex.load(session=session, cache=cache, refresh=True)


@pytest.fixture
def http_index(tmp_path, monkeypatch, small_index):
    monkeypatch.setattr(index_mod, "INDEX_RETRY_DELAY", 0)
    monkeypatch.delenv("PIPSEEK_INDEX_URL", raising=False)
    src = tmp_path / "idx.tsv.gz"
    _write_index(src, 50)
    data = src.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def test_index_download_retries_while_the_asset_is_swapped(tmp_path, http_index):
    data, digest = http_index
    session = _HttpIndex(data, missing=2, checksums=[digest])
    assert _http_load(tmp_path, session).source == "full"
    assert session.index_calls == 3


def test_index_download_gives_up_after_retries(tmp_path, http_index):
    data, digest = http_index
    session = _HttpIndex(data, missing=5, checksums=[digest])
    assert _http_load(tmp_path, session).source == "names"


def test_checksum_is_read_again_when_it_was_published_late(tmp_path, http_index):
    data, digest = http_index
    session = _HttpIndex(data, checksums=["0" * 64, digest])  # old checksum, then the new one
    assert _http_load(tmp_path, session).source == "full"


def test_wrong_checksum_is_still_rejected(tmp_path, http_index):
    data, _ = http_index
    session = _HttpIndex(data, checksums=["0" * 64])
    assert _http_load(tmp_path, session).source == "names"
