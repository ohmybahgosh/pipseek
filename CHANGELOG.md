# Changelog

## 2.2.2 (2026-11)

### Changed

- pipseek now checks once a day whether a newer search index is out, instead of downloading a new one once a week.  The check fetches only the index's 84-byte checksum, and the 35 MB index is downloaded only when it changed, so a newly published package shows up in search within about a day instead of up to a week.  When the check can't be made, the local copy is used for up to a week, as before.
- When a newer index can't be downloaded or swapped in (for example while another pipseek has the database open on Windows), pipseek keeps its copy and tries again the next day instead of on every run.

## 2.2.1 (2026-11)

### Fixes

- The index download retries for a few seconds when the file is briefly missing.  The daily job swaps in a new index by deleting the old file and renaming the new one, which leaves about a second with no file.
- The index and its checksum are published one after the other.  When a download lands between the two, pipseek reads the checksum again instead of rejecting a good index.

### Project

- A new weekly "Index watchdog" workflow keeps the scheduled jobs from being switched off after 60 quiet days, even when the index build is failing, and fails (which emails the owner) when the published index is more than 3 days old.

## 2.2.0 (2026-11)

Fixes from a full review of 2.1.0.  Some command-line behavior changed, so this is 2.2.0 instead of 2.1.1: `--strict` has a new exit code, and flags that used to be ignored now stop with an error.

### Security

- Text from PyPI (summaries, authors, licenses, URLs, keywords, extras) and your own search terms are now always shown as plain text.  Before, square brackets were read as terminal markup.  A summary like `qiniu://<bucket>[/name_of_dir]` crashed every search it showed up in, 488 other real summaries lost words on screen (`agentassert-abc[claude-code]` showed as `agentassert-abc`), and a package author could hide links or fake badges.  Searching for `[/]` crashed too.
- The search index download is checked against a published SHA-256 checksum, capped in size, and rejected when most summaries are missing.
- The PyPI publish action in the release workflow is pinned to a commit.

### Fixes

- "Installed" badges are reliable again.  Search looks up packages on several threads, and all but the first could see an empty list of installed packages, so `pipseek --json requests` often reported `installed_version: null`, and the red "your installed version is vulnerable" badge could go missing.
- `pipseek --outdated` no longer reports "no known vulnerabilities" when it couldn't check.  It lists the packages PyPI or OSV.dev couldn't be reached for, shows "couldn't check" in the table, and uses `null` instead of `[]` in `--json` output.
- Offline, search shows matches from the local index (name and summary) instead of "No packages match", and `--info` does the same instead of "not found on PyPI".  Expired cached details are used when PyPI can't be reached, and a failed lookup no longer silently shortens a page of results.
- A truncated or corrupt cache file no longer crashes every run.  It's deleted and fetched again.
- `--clear-cache` now removes the search database (about 175 MB) and leftover temp files too, as the README said.
- The first index build streams the download into the database instead of holding it all in memory.  Peak memory for a first run dropped from about 1 GB to about 60 MB.
- Temp files get unique names, so two pipseek runs at once can't overwrite each other's.  On Windows, if another pipseek has the database open, the old one is kept instead of crashing.  A failed index download no longer replaces a full index with the names-only fallback.
- Conda on Windows (`python.exe` at the env root) is detected, the Microsoft Store `python` placeholder is skipped, and upgrade commands quote paths with spaces.
- Searches in other scripts work (`pipseek "ελληνικά"` used to find nothing).
- When download statistics can't be fetched, pipseek waits 6 hours before trying again instead of stalling every run.

### Changed

- `--outdated --strict` exits 2 when some lookups failed, so CI doesn't pass on a check that never happened.  1 still means something installed is vulnerable.
- `--no-stats` now also skips vulnerability checks in `--outdated`, as its help text said.  It can't be combined with `--strict`.
- `--all` and `--strict` stop with an error when used without `--outdated`, instead of being ignored.
- `PIPSEEK_INDEX_URL` accepts a local path or a `file://` URL as well as http(s), and is listed in `--help`.

### Project

- CI checks out with read-only permissions, tests Python 3.14 too, and builds the package and runs `twine check` on every push.
- The sdist no longer includes screenshots and CI files (6.4 MB down to about 0.7 MB), and the package lists the Python versions it supports.
- The daily index job fails when the previous index can't be downloaded, instead of quietly starting over, and won't publish an index that's missing most summaries.  It uploads under a temporary name and then swaps, so the download never disappears mid-upload, publishes a `.sha256` next to it, and keeps its schedule from being switched off after 60 quiet days.

## 2.1.0 (2026-11)

### Security

- Known vulnerabilities from OSV.dev (free, no key) for the latest version and for the version you have installed: severity, CVE and the version that fixes each one.  Duplicate advisories (GHSA, PYSEC and CVE ids for the same problem) are merged.
- A red badge in the detail pane and a flag in the results when your installed version is affected.
- `pipseek --outdated` checks your whole environment for outdated and vulnerable packages, worst first, and ends with the upgrade command (vulnerable packages first when the list is long).  `--all` lists every package, `--json` gives structured output, and `--strict` exits with status 1 when something installed is vulnerable, for CI.
- On an OS-managed ("externally managed") system Python, pipseek no longer suggests `pip install` commands or installs with the `i` key.

### Other

- Fix versions prefer final releases over release candidates.
- Releases are published to PyPI from GitHub Actions with Trusted Publishing, so no API token is stored anywhere.
- The README now uses absolute image links, so it renders on PyPI too.

## 2.0.0 (2026-11)

A ground-up rewrite.  Written with AI coding agents (Claude), directed, reviewed and tested by [OhMyBahGosh](https://github.com/ohmybahgosh).

### Search

- Replaced scraping of `pypi.org/search` with a self-hosted search index built from PyPI's official Simple and JSON APIs.  v1 got past PyPI's bot challenge to scrape the search page; v2 doesn't touch that page at all, so it can't break when the challenge changes.
- Searches project summaries and keywords, not just names, through a local SQLite FTS5 database.
- Ranking blends name matches, BM25 relevance and 30-day download counts.  Tuned against the full index of about 930,000 projects so that empty placeholder packages named after a search term no longer outrank Django or tqdm.
- "Did you mean?" suggestions for typos.
- A daily GitHub Action rebuilds the index incrementally, re-fetching only projects whose PyPI serial changed, and publishes it on the `index` release.
- Summaries for the 15,000 most popular projects ship inside the package as a fallback.  When pipseek only has the fallback, it tries for the full index again every day instead of every week.

### Interface

- New Textual TUI: search box, results list, a detail pane that follows the cursor, five sort orders, paging, clipboard copy, browser shortcuts and one-key install or upgrade.
- `--plain`, `--json` and `--info` modes for scripts.  Piped output switches to plain mode automatically.
- Installed and update-available badges for your active environment, even when pipseek itself runs from pipx or uv.

### Data

- Stars, forks, open issues and the OpenSSF Scorecard come from deps.dev, which needs no API key and covers GitHub, GitLab and Bitbucket.  This replaced the GitHub API and its 60-requests-an-hour limit; `GITHUB_TOKEN` is no longer used.  `--no-github` is now `--no-stats` (the old name still works).
- Release dates, authors and licenses now handle the many ways packages fill in (or leave out) their metadata.

### Packaging and quality

- Installable with pipx, uv or pip, with a real `pipseek` command, replacing the loose `pipseek.py` script.
- pytest suite and ruff linting, run in CI on Linux, macOS and Windows with Python 3.9 and 3.13.
- Fixed before release: the plain-table mode crashed on start after an option was renamed.  Caught by running it on a real desktop, and now covered by a test.

## 1.0.0 (2025)

- The original single-file script: scraped PyPI's search page (solving its proof-of-work challenge, based on [work by Long0x0](https://github.com/victorgarric/pip_search/issues/44#issuecomment-2565442916)) and showed results in a Textual app.
