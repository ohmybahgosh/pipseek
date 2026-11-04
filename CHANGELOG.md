# Changelog

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
