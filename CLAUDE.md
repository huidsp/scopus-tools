# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`scopus_tools` is a Python toolkit over the Elsevier Scopus REST API, the KAKEN (科研費) API
and the Web of Science Starter API.
It fetches researcher data, computes bibliometric indicators (H-index, G-index, citations,
first-author counts), pulls KAKEN grant histories, and annotates papers with their
Web of Science index coverage. It ships two frontends:

- A CLI (`scopus-tools`) with 13 subcommands.
- An MCP server (`scopus-tools mcp`, stdio) exposing data retrieval plus project persistence.

**This package never calls an LLM over an API.** Evaluation, field inference, and
researcher comparison are the MCP host model's job — it calls these tools iteratively and
reasons itself. A previous version had `ai_engine` / `llm` modules and `analyze` / `eval`
subcommands backed by the OpenAI and Anthropic SDKs; they were deliberately removed in
v0.4.0, along with the Gradio WebUI. **Do not reintroduce them** — no `openai` /
`anthropic` dependency, no "let the tool call a model for you" feature. A test in
`tests/test_mcp_server.py` (`test_package_has_no_llm_api_modules`) guards this.

The CLI is primarily Japanese-facing (report text and many log messages), but identifiers
and CLI flag names stay in English.

## Environment

- Python ≥ 3.12 (`requires-python` in `pyproject.toml`). Dependencies are only `requests`
  and `python-dotenv`. Optional extras: `[dev]` → `pytest`; `[mcp]` → `mcp>=1.2`.
  **pandas was removed in v0.6.0** — it and numpy were 104MB of a 174MB install for what
  amounted to four CSV helpers. CSV I/O is stdlib `csv` now (16MB CLI / 54MB with MCP).
  Don't reintroduce pandas for CSV work.
- `.env` is searched in this order by `cli._load_env_files()`, first hit wins: the current
  directory (upwards), then **`~/.scopus-tools/.env`**, then the package location (upwards,
  which only resolves for editable installs). The middle one is the canonical home — a
  `uv tool install` user has no repo clone, and it sits beside the cache DB and projects.
  Three keys:
  - `SCOPUS_API_KEY` — required for any Scopus-touching command.
  - `KAKEN_APP_ID` — required for KAKEN-touching commands.
  - `WOS_API_KEY` — required for the `wos` command and the two WoS MCP tools.
    Register at developer.clarivate.com **from the subscribing institution's network**;
    the plan (and therefore the quota, and whether Times Cited is returned) is decided
    by the IP you register from.
- Local dev install:

  ```bash
  python3.12 -m venv .venv && source .venv/bin/activate
  pip install -e ".[mcp,dev]"
  ```

## Common commands

```bash
# CLI (after editable install)
scopus-tools <subcommand> ...

# or without install
python -m scopus_tools.cli <subcommand> ...

# tests
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m pytest tests/test_scopus_tools.py::TestComputeIndices::test_basic -v
```

CLI subcommands (see `scopus_tools/cli.py`):

- `search` — Scopus ID lookup. `--first/--last` is one request; `--name` splits on whitespace
  assuming *given surname*; `--try-both` restores the old two-request behavior. Batch CSV takes
  `First Name`/`Last Name` columns, else falls back to splitting `Name`.
- `stats` — paper / citation counts per year range; requires `--years`.
- `summary` — human-readable per-author report; supports `--input` CSV mode and `--format json`.
- `papers` — list papers published in a given `--years` range; positional IDs or `--input` CSV,
  `--format text|json|csv` (csv requires `--output`). Each paper carries `author_position` /
  `author_count` (shown as `2/3`, `1/4 (first)`) computed in `api.search_papers`.
  `--scie-list CSV [CSV ...]` annotates each paper with the matching Web of Science index
  names (SCIE/SSCI/AHCI/...) by ISSN match (see `scie`); `--scie-only` then keeps only papers
  indexed in at least one given list.
- `batch` — CSV-in / CSV-out summary across many authors.
- `kaken-search` / `kaken-summary` — KAKEN researcher lookup and grant summary.
- `mcp-setup` — registers this tool with Claude Code (`claude mcp add`) or Claude Desktop.
  Touches no network. See the `mcp_setup` module note below.
- `cache` — `stats` / `list` / `clear` / `vacuum` / `path`. Touches no network.
- `wos` — Web of Science lookups. `--rid` (ResearcherID/ORCID), `--name [--org]`,
  `--doi`/`--title`, `--years`, `--limit`. See the `wos` module note on which of the two
  author-matching strategies to trust.
- `mcp` — runs the MCP server over stdio (see `mcp_server`); takes `--projects-dir`,
  `--scie-list CSV [CSV ...]`, and `--scie-dir DIR` (loads every `*.csv` in DIR). With neither
  scie flag it auto-detects `*Citation Index*.csv` and `index/*.csv` in the launch dir.
  Forces logging to stderr before starting.

All subcommands also accept the global cache/network flags: `--refresh`, `--offline`,
`--no-cache`, `--cache-db PATH`, `--timeout SEC`, `--stale-days N`, and the repeatable
`--stale-days-for API=N`.

Year-range arguments accept `2021-2025`, `2021,2025`, `2021:2025`, or `[2021,2025]`
(parsed by `core.parse_year_range`; `cli._parse_year_range` wraps it to turn `ValueError`
into `parser.error`). When omitted, defaults to the **previous year inclusive,
last 5 years** via `core.default_eval_year_range()` (e.g., in 2026 → `(2021, 2025)`).

## Architecture

The codebase is intentionally flat — modules under `scopus_tools/`, each with a single responsibility.
Two control flows:

**CLI**: `cli.py` → `ScopusClient` / `KakenClient` / `WosClient` → `core` (pure functions)
→ `utils` (presentation).
**MCP**: `mcp_server.py` → `ScopusClient` / `KakenClient` / `WosClient` / `ProjectStore`
→ `core` / `scie` / `linking` → JSON dicts (no `utils` presentation layer).

Each client sends through its own `httpcache.HttpLayer`, minted from a shared
`HttpContext` (one SQLite cache + one `requests.Session` per process). Scopus and KAKEN
authenticate with a query parameter (`auth_params`); WoS uses a header (`auth_headers`).
Both are injected after the cache key is computed, so neither reaches the DB.

### Modules

- **`httpcache`** — the single outbound HTTP seam for **all three** clients. Timeout, connection
  pooling, throttle, 429/5xx retry, and the SQLite response cache all live here.
  - **Never write secrets to disk.** `apiKey`/`appid` live in `HttpLayer(auth_params=...)` and
    are injected *after* the cache key and `params_json` are computed, so they cannot reach the
    DB. `canonical_params()` also denylists them, and `tests/test_httpcache.py` dumps every
    column asserting the key never appears. Keep all three defences.
  - `_raw_get()` is a **module-level function on purpose**: with `session=None` it falls through
    to `requests.get`, so the tests that patch `requests.get` keep working. Production passes a
    `requests.Session`.
  - `HttpResult` is *not* a `requests.Response`. It offers `.status_code` / `.content` /
    `.text` / `.json()` only — check before reaching for `.ok` or `.raise_for_status()`.
  - 429 + `X-ELS-Status: QUOTA_EXCEEDED` raises `QuotaExceeded` immediately and records
    `quota_blocked`; later calls fail fast until the reset. **Never sleep for a quota reset** —
    it can be days away. A 429 without that header is the per-second throttle and *is* retried.
  - `HttpLayer.collect()` gathers `(api, fetched_at, cached)` for the requests made inside it;
    that is how an operation's as-of date is derived (the **oldest** contributing request).
  - Cache failures must never break a fetch: every `CacheDB` call goes through `_safe()`.

- **`cachedb`** — SQLite persistence, no HTTP knowledge. `~/.scopus-tools/cache.sqlite3`
  (`$SCOPUS_TOOLS_CACHE_DB` / `--cache-db` override; `$SCOPUS_TOOLS_CACHE_DISABLE=1` disables).
  WAL + `busy_timeout` so the CLI and the MCP server can share it. `CHECK (status = 200)`
  enforces "never cache a failure" at the schema level. A corrupt or version-mismatched DB is
  moved aside and recreated — a cache is regenerable, so never block the user on migration.
  - **Caching is HTTP-request-level only, deliberately.** There is no operation-level cache: if
    page 2 of a paginated fetch fails, pages 1 and 3 are cached and the next run refetches only
    page 2 — self-healing. An operation-level cache could persist a truncated paper list as
    canonical, which would silently understate someone's publication record.

- **`asof`** — freshness and as-of-date consistency. **Nothing ever auto-expires**: for
  personnel review the compared researchers' data must share a fetch date, so refetching is
  explicit (`--refresh` / `refresh=true`) and staleness is only *warned* about.
  - Thresholds are **per API family** (`DEFAULT_STALE_DAYS`): `scopus_search` 30 days
    (citation counts move, but only meaningfully month over month), author/researcher lookups
    90 (name→ID mappings barely change). Comparison-set consistency is guarded separately by
    `SPREAD_TOLERANCE_DAYS` (1 day), so this threshold does not need to be short. `StalePolicy`
    resolves defaults < `$SCOPUS_TOOLS_STALE_DAYS` < `--stale-days` < `--stale-days-for API=N`.
  - `spread()` flags a comparison set whose fetch dates differ by more than a day. An unknown
    fetch date is never treated as fresh.

- **`api.ScopusClient`** — the only Scopus network boundary.
  - `_cover_year()` parses `prism:coverDate` defensively, returning 0 when it is absent,
    null, empty or malformed. `entry.get("prism:coverDate", "0000")[:4]` was not enough —
    the default only applies when the **key** is missing, so a null value raised `TypeError`
    and an empty string `ValueError`, from inside the `search_papers` pagination loop, where
    one bad record killed the whole author fetch. `core.summarize_papers` then excludes
    `year == 0` from `start_year`, since including it made `research_years` come out as
    `current_year + 1`.
  - `parse_entry(entry, author_ids=None, detail=False, include_abstract=False)` turns one
    Scopus entry into a paper dict, shared by every fetch path.
    - **`author_ids=None` leaves `is_first_author` and `author_position` as `None`**, not
      `False`/0 — `find_papers` does not know whose paper it is, and a `False` there reads as
      "not the first author", which is a different claim from "unknown".
    - Cheap, high-value fields (`doi`, `article_number`, `open_access`) are on **every** path.
      Everything expensive (`authors_detail` with Scopus author IDs, `affiliations`,
      `keywords`) is behind `detail=True`, and the abstract behind `include_abstract`,
      because `list_papers` returns up to 200 papers and per-paper size is the response size.
    - `auth_list` / `authors` must keep their exact shape — `utils.print_papers_list`, the CSV
      export and `tests/test_legacy_compat.py` depend on them. Add, never change.
  - `find_papers(title=, doi=, author_last_name=)` ANDs whatever is given into
    `TITLE(...) AND DOI(...) AND AUTHLASTNAME(...)`. Its purpose is resolving **split author
    profiles**: pull one missing paper, read the `authid` out of `authors_detail`, then pass
    both IDs to `author_summary` comma-separated (dedup by `eid` already merges them).
    - Scopus title matching is loose — measured: leading words alone, punctuation, and even a
      one-word misspelling all hit. Never assume a single result; return several and let the
      caller judge.
    - It always requests `count=FIND_PAGE_SIZE` (25) and slices to `limit` locally. The cache
      key covers every parameter, so a variable `count` would create a separate cache entry
      per `limit` and refetch the same papers.
    - Quota/throttle stays `scopus_search`. The 30-day staleness threshold is left as-is even
      though authids never change, because the same response carries citation counts.
  - `search_papers_detailed()` returns a `FetchResult` carrying **`complete`**; `search_papers()`
    returns just the list (unchanged contract). Incomplete means Scopus did not give us
    everything — a non-200 mid-pagination, the 5,000-record pagination ceiling, an empty page
    before the reported total, or a count shortfall beyond `PAGINATION_TOLERANCE`. Partial
    results are still returned (no exception, as before) but callers **must** surface the fact:
    a truncated list presented as a full publication record understates someone's output.
    Note this is distinct from MCP's `truncated`, which just means we applied `limit`.
  - `search_author(first_name, last_name)` is the primary entry point and issues **exactly one
    request**. Author Search is the tightest Elsevier quota (**5,000/week, 2 req/s**), so we do
    not spend two requests guessing the name order. If the caller gets the order wrong they
    retry with the arguments swapped — the MCP host model can decide this, and the MCP tool's
    zero-hit response carries a `hint` telling it to.
  - `search_author_by_name(name, try_both=False)` splits on whitespace assuming
    `given surname` and delegates. **This reverses an earlier deliberate design**: the old code
    always tried both orderings because the CLI had no intelligence to pick one. `try_both=True`
    preserves that behavior for the genuinely-unknown case, at double the quota cost.
    `tests/test_legacy_compat.py` pins both behaviors.
  - `search_papers` paginates with `view=COMPLETE`, dedupes by `eid`, takes `max(citations)`
    on duplicates, and ORs `is_first_author` flags (computed by matching the entry's first
    `authid` against the queried `author_ids` set).
  - `search_papers` paginates with `view=COMPLETE`, dedupes by `eid`, takes `max(citations)`
    on duplicates, and ORs `is_first_author` flags (computed by matching the entry's first
    `authid` against the queried `author_ids` set).

- **`kaken.KakenClient`** — the only KAKEN network boundary. Wraps NRID (researcher) and KAKEN
  (project) OpenSearch endpoints. NRID returns JSON, KAKEN returns XML — both parsed into
  flat dicts. Requires `KAKEN_APP_ID`.
  - **NRID responses are huge and must be narrowed at the parse step.** Each researcher item
    embeds that person's entire publication list (`work:product`), ~1 MB each; 50 prolific
    researchers measured at **29 MB**. `_parse_researcher_json` therefore keeps only the
    identification fields (plus `project_count` / `product_count` as counts) and deliberately
    does **not** carry the API item through — an earlier `raw` key made the MCP
    `kaken_search_researcher` response 25 MB and the tool call timed out client-side.
    `search_researcher_by_name` also defaults to `rw=20`, the smallest page NRID accepts
    (valid values are 20/50/100/200/500 — smaller ones silently return nothing).
    Grant details come from `get_grants_by_researcher_id`, which is small (~19 KB for 19 grants).

- **`wos.WosClient`** — the only Web of Science network boundary (Starter API).
  Authenticates with the `X-ApiKey` **header**, which is why `HttpLayer` grew
  `auth_headers`; request headers are in neither the cache key nor the DB, so the
  guarantee matches the Scopus query-parameter path.
  - **Starter does not return the SCIE/SSCI/AHCI/ESCI edition.** Neither `Document` nor
    `Journal` has such a field, and `citations[].db` is the *database* ("WOS", "MEDLINE"),
    not the edition. So the API **cannot** replace `scie.py`'s Master Journal List ISSN
    matching — that was the hoped-for win and it is not available. Also absent from
    Starter (Expanded only): abstracts, author addresses, funding, cited references.
  - Measured limits, visible in `X-RateLimit-Remaining-Day` / `-Second`: Free Trial
    1 req/s and 50/day; **Institutional 5 req/s and 5,000/day**; Institutional
    Integration 20,000/day. Max 50 records per request (51 → HTTP 400). Paging is deep —
    verified to record 50,000, unlike Scopus's 5,000 ceiling — so `MAX_PAGES` (40) is
    our own guard against burning the daily budget, not an API limit.
  - **Two ways to identify an author, with opposite failure modes. Measured on one real
    researcher whose Scopus record is 244 papers:**
    - `AI=` (ResearcherID *or* ORCID — both work): **80 records**. Precise, but WoS only
      links records whose ResearcherID was claimed, so this is a **lower bound**.
    - `AU=` + `OG=`: **255 records**, of which **176 were a different person** (1970s
      general-relativity papers) and **none of those 176 carried the ResearcherID**.
      `AU=` alone was 2,996.
    Neither number is the researcher's output. `wos_author_documents` therefore always
    returns a `caveat` naming the strategy and its failure mode, plus an extra `warning`
    when a name search omits the organization. Keep that — a bare count from either
    strategy is wrong in a way that a personnel review will not catch.
  - `parse_document()` returns **the same dict shape as `api.parse_entry`**, so `core`,
    `scie` and `utils` work on WoS papers unchanged; `source: "wos"` distinguishes them.
    `tests/test_wos.py` asserts the key sets stay compatible. WoS Times Cited and Scopus
    citation counts are different corpora — never mix them into one indicator.

- **`core`** — pure functions, no I/O.
  - `compute_indices(citations)` → `(h, g)`.
  - `summarize_papers(papers, year_range)` returns a dict consumed by `utils.print_report_text`
    and `utils.process_batch_summary` — keep the keys in sync if you change either.
  - `resolve_year_range` (internal default) and `default_eval_year_range` (user-facing default
    for the UI/CLI: previous year inclusive, 5-year window).

- **`linking`** — Scopus author → KAKEN researcher number matching by name. Its only caller is
  the MCP `link_kaken_researcher` tool. All of its output goes to **stderr**, and MCP passes
  `interactive=False` so it never hits the `input()` branch — keep both properties if you edit it.

- **`scie`** — Web of Science indexing check (SCIE/SSCI/AHCI/ESCI). Scopus has no WoS-index
  flag (that's a Clarivate/Web of Science concept), so this matches a paper's `issn`/`eissn`
  (added by `api.search_papers`) against **user-supplied** per-index journal lists.
  - `load_scie_issn_set(path)` — reads a CSV (auto-detects columns whose name contains `issn`;
    falls back to all columns / column names for headerless files) or a one-ISSN-per-line text
    file, returns a set of `normalize_issn`'d ISSNs.
  - `normalize_issn(v)` strips hyphens/spaces, uppercases, requires 8 chars (else `None`).
  - `derive_index_label(path)` — index name from the filename's parenthesized abbreviation
    (`... (SCIE).csv` → `SCIE`), else the filename stem.
  - `load_index_sets(paths)` — `{index_label: issn_set}` from several lists (same label merges).
  - `annotate_papers_indexes(papers, index_sets)` sets `wos_indexes` (sorted label list) and
    `is_scie` (`"SCIE" in wos_indexes`) in-place; returns the count of papers in ≥1 index.
  - `annotate_papers(papers, issn_set)` — legacy single-set helper setting just `is_scie`.
  - `resolve_index_paths(scie_list, scie_dir)` / `discover_index_sets(...)` — shared
    startup loader (precedence: explicit list → all `*.csv` in dir → auto-detect
    `*Citation Index*.csv` + `index/*.csv`). Called from `mcp_server.run`; the CLI passes
    explicit paths via `load_index_sets`. Don't re-implement the glob logic elsewhere.
  - Lists are登録制 (Clarivate Master Journal List), one CSV per index, not auto-downloadable;
    pass them via `--scie-list`. Each index is a separate download — a single CSV has no
    per-row index label. The data files are gitignored.

- **`utils`** — CSV I/O (`utf-8-sig` output for Excel compatibility), logging setup,
  per-row progress helpers (`progress`, `progress_done`), and the two batch drivers
  (`process_author_csv`, `process_batch_summary`). Per-row error handling lives here —
  missing Scopus IDs are logged and skipped, not raised. Shared formatter helpers
  (`_hr`, `_section`) are used across the `print_*` functions.

- **`projects`** — JSON-file project store, reached through the MCP project tools.
  **Hierarchical model**:
  - Project (a department / committee / cohort) contains a `researchers` list.
  - Each researcher has `scopus`, `kaken`, `ai` sections.
  - Project also has a top-level `comparison` section for cross-researcher analysis.
  - `ProjectStore` does CRUD with atomic writes (`tempfile + os.replace`).
  - `_migrate_if_legacy()` auto-converts old flat-format files (a single researcher per project)
    on load — don't remove this until you're sure no old files exist.

- **`mcp_server`** — MCP (stdio) frontend, 17 tools (the list lives in `_TOOLS`):
  - Data retrieval: `search_author`, `author_profile`, `author_summary`, `list_papers`,
    `kaken_search_researcher`, `kaken_grants`, `link_kaken_researcher`,
    `wos_find_document`, `wos_author_documents`.
  - Project persistence: `list_projects`, `read_project`, `create_project`, `delete_project`,
    `save_researcher_section`, `save_comparison` — thin wrappers over `projects.py`.
  - **No evaluation tool, by design** — the host model calls the retrieval tools iteratively
    and reasons itself, rather than a tool nesting a second LLM call. Don't "complete the API"
    by adding an `evaluate` / `compare` tool; `tests/test_mcp_server.py` guards the tool list.
  - Tools are plain module-level functions (testable without the MCP protocol layer) that
    return dicts. Missing API keys produce `{"error": ...}` rather than an exception, so the
    host model can relay the problem. Clients and the `ProjectStore` are lazily constructed in
    `_get_scopus` / `_get_kaken` / `_get_store`, so the server starts without any keys.
  - **`@_network_guard` extends that contract to network failures.** Every retrieval tool
    carries it; it converts `QuotaExceeded` / `OfflineError` / `RateLimited` /
    `requests.Timeout` / `requests.ConnectionError` into `{"error": ..., "retriable": ...}`.
    Only the missing-key case used to return a dict, so the far likelier failure — quota
    exhaustion (Author Search is 5,000/week) — escaped as an exception, which the host sees
    only as a protocol error, leaving the model to retry with different arguments forever.
    `retriable=False` on quota/offline tells it retrying is pointless. Keep new retrieval
    tools decorated.
  - **A failed fetch must never read as "nothing found".** `find_papers` only attaches its
    "No match, try fewer words" hint when `fetched.complete` — otherwise a 401 sent the model
    off rewording titles indefinitely. `_attach_completeness(..., paginated=False)` likewise
    drops the "pagination stopped at the failing page" sentence for the single-request paths,
    and never prints `about None` when Scopus reported no total.
  - `_server_class()` absorbs the SDK rename: MCP SDK 2.x has `mcp.server.MCPServer`,
    1.x has `mcp.server.fastmcp.FastMCP`; `add_tool` / `run(transport=...)` are identical.
  - **stdout is the JSON-RPC channel** — never `print()` to stdout from anything reachable
    here. `utils.progress` already writes to stderr; `cli` calls
    `utils.setup_logging(stream=sys.stderr)` (which passes `force=True`) before starting.
  - `list_papers` caps results at `DEFAULT_PAPER_LIMIT` (200) and reports `truncated`.
  - Every retrieval tool takes `refresh=False` (a **per-call** override — never set it on the
    layer, it would leak into later calls) and returns `as_of` + `as_of_note`. The prose note
    matters: models relay a sentence far more reliably than a raw timestamp.
  - `read_project` / `save_comparison` attach `as_of_warning` when the researchers' fetch dates
    differ by more than a day. It **warns, never blocks** — that was an explicit decision.
  - Tool docstrings are sent to the model as the tool description — keep them useful.

- **`config`** — reads and writes `~/.scopus-tools/.env`, behind `scopus-tools config`.
  Always writes mode 600 (the file holds API keys in plain text) via the same
  `tempfile` + `os.replace` pattern used elsewhere, merges rather than replaces, and keeps
  keys it does not own so the file is not monopolised. Unknown key names are rejected rather
  than silently written, so a typo cannot look like success. `config KEY` without a value
  prompts via `getpass` — a key passed on the command line lands in shell history.
  `user_env_path()` here is the single definition; `cli` and `mcp_setup` both delegate to it.

- **`mcp_setup`** — automates MCP client registration. Exists because two things reliably
  break a hand-written registration, both verified empirically:
  - **The command must be an absolute path.** MCP clients spawn the server without a shell,
    so no venv activation and no `PATH`. `resolve_command()` resolves `sys.argv[0]` through
    `os.path.realpath` (uv tool installs are symlinks), then `shutil.which`, then
    `python -m scopus_tools.cli`.
  - **Shell `export` does not reach the server.** Keys must be embedded at registration time.
    `build_entry(with_keys=True)` reads them from the environment/`.env`; the CLI then
    **always prints which file received the key in plain text**. `--no-keys` opts out.
  - Config writes take a `.bak`, write atomically (`tempfile` + `os.replace`, mode 600),
    then **re-read and verify no pre-existing top-level key or other `mcpServers` entry was
    lost** — restoring from the backup and aborting if any were. Corrupt JSON is never
    overwritten. Keep these guarantees; `tests/test_mcp_setup.py` pins them.
  - Claude Code goes through the `claude mcp add` CLI rather than editing its config, so
    scope handling stays with the official implementation. `claude mcp add` has no overwrite
    flag and exits 1 with "already exists", so `run_claude_code` removes and re-adds in that
    one case — re-running with changed settings is normal, and the Claude Desktop path
    already replaced in place.
  - **`tcc_warnings()` refuses to register a path under `~/Documents`, `~/Desktop` or
    `~/Downloads`.** Those are TCC-protected on macOS and a GUI client cannot read them: a
    venv there dies with `PermissionError: ... pyvenv.cfg` before Python finishes starting.
    This actually happened — Claude Code (terminal, has permission) connected fine while
    Claude Desktop could not start the server at all, so it is easy to miss. Install via
    `uv tool install` (`~/.local`) and keep the index CSVs in `~/.scopus-tools/index`.
    Only paths **read at run time** matter: uv builds a wheel and copies it into `~/.local`,
    so the source clone may sit in `~/Documents` — the installed tree holds no reference back
    to it. The index CSVs are different: they are opened on every start, so their location
    does matter.

- **`cli`** — argparse dispatch only; no business logic.
  - `KEY_REQUIREMENTS` is a static map consumed by `_check_required_keys`. `mcp` maps to an
    empty list on purpose: it validates keys per tool call, not at startup.
  - `_collect_targets` centralizes positional `ids` vs `--input` CSV mode for `summary`/`papers`.
  - `YEAR_RANGE_HELP` is derived from `core.YEAR_RANGE_HELP` — keep the single source.

## Tests

`tests/` uses `pytest` and mocks at the right boundaries:

- `tests/test_scopus_tools.py` — primary suite. Mocks `ScopusClient` at the `requests.get`
  level, uses the `DUMMY_PAPERS` fixture. Also covers CLI dispatch (`_DUMMY_ENV`).
- `tests/test_projects.py` — `ProjectStore` CRUD + legacy migration tests.
- `tests/test_mcp_server.py` — MCP tool functions called directly (the protocol layer is not
  exercised, so these need no MCP SDK). Covers the missing-key error path, `list_papers`
  truncation, the project roundtrip on `tmp_path`, `core.parse_year_range`, and guards both
  the exposed tool list and the absence of the LLM modules.
- `tests/test_legacy_compat.py` — guards backward-compatible behavior against the
  pre-refactor scripts in `old/` (gitignored). Also pins the single-request author search
  and the `try_both=True` escape hatch.
- `tests/test_cachedb.py` / `tests/test_httpcache.py` — SQLite layer and the HTTP seam,
  including the **secret-leak test** (dump every column, assert the key is absent) and the
  quota/throttle/retry behaviors.
- `tests/test_completeness.py` — the truncation guards. Highest-value file here.
- `tests/test_asof.py` — threshold resolution order and comparison-set consistency.
- `tests/test_mcp_setup.py` — registration path resolution and the config-preservation
  guarantees (other servers survive, key loss rolls back, corrupt JSON is refused).
- `tests/conftest.py` — an autouse fixture disables the cache and points `HOME` at `tmp_path`,
  so the suite never touches the real `~/.scopus-tools` and no test can be served another
  test's cached 200. It also exports `make_response()`, which fills `.content` **and**
  `.json()` because the cache stores raw bytes.

`.github/workflows/test.yml` runs the suite on Python 3.12 for every push and PR.

## Notes

- `old/` contains pre-refactor standalone scripts and is gitignored. Reference only; don't import.
- Output CSVs use `utf-8-sig` so Excel renders Japanese correctly — don't switch to plain
  `utf-8` without reason.
- Log progress messages in `search_papers` are intentionally in Japanese ("Scopus取得進捗…");
  the report text in `utils.print_report_text` is also Japanese. UI strings are not internationalized.
- The MCP server's stdout is the JSON-RPC channel. If you add a code path reachable from a
  tool, make sure it writes to stderr — a single stray stdout line breaks the handshake.
- **Docker support was removed in v0.8.0.** The image existed to hand the tool to people
  without Python, but `uv tool install` covers that in one command and is what the MCP
  registration wants anyway (a stable absolute path). Running under Docker also meant Docker
  Desktop had to be up before Claude Desktop could start the server, ~1.6s of container
  startup per session, and three volumes to mount correctly. Images already on GHCR stay
  published; they are simply no longer updated.
- **Packaging as a macOS `.app` was considered and rejected** (v0.6.0). The sibling project
  `../Secretary` does bundle one, but every reason it needs to — a WKWebView GUI, TCC/Full
  Disk Access attributed to the bundle, a self-signed cert purely to stop ad-hoc cdhash churn
  from revoking FDA, SMAppService — is absent here; this is a headless network client. The
  costs are real and documented in that repo: no notarization (Gatekeeper blocks it on other
  Macs), no auto-update, no CI, rebuilds silently breaking the launchd registration, and an
  MCP registration pointing at a hardcoded `/Applications/...` path that is no better than the
  `~/.local/bin/scopus-tools` you get from `uv tool install`. Handing the tool to someone else
  is answered by `uv tool install ... @ git+https://...` — asking them to install uv is a far
  smaller imposition than notarization and a release pipeline. Don't revisit without a new reason.
- **Incremental / delta fetching from Scopus was investigated and rejected.** Measured against
  the live API on a 244-paper author:
  - `RECENT(30)` is **silently ignored** — it returns HTTP 200 with the unfiltered count. A
    query that looks like it filters but does not is worse than one that errors.
  - `LOAD-DATE AFT yyyymmdd` **does** work (undocumented in the API search tips) and is
    distinct from `ORIG-LOAD-DATE`: over 30 days, 28 records had a new LOAD-DATE while 0 had a
    new ORIG-LOAD-DATE, so LOAD-DATE tracks updates to existing records.
  - **But nothing documents that it covers citation-count changes**, and a false "nothing
    changed" would silently feed stale citation counts into a personnel evaluation.
  - The quota does not justify the risk anyway: re-fetching 244 papers is 10 requests out of
    20,000/week, i.e. ~2,000 full researcher re-fetches per week. Just re-fetch.
- API keys: `.env` (chmod 600) or the MCP client's config `env` (those files are already 600).
  **Not** the macOS Keychain — an unsigned CLI spawned by varying parent processes hits
  repeated authorization dialogs, and it would be macOS-only.
- `utils.silence_url_logging()` clamps `urllib3`/`requests`/`httpx`/`httpcore` to WARNING.
  Scopus passes `apiKey` as a **query parameter**, so those libraries print the key in full at
  DEBUG level — and MCP client log files are not always private. It is called from both
  `setup_logging()` and `HttpLayer.__init__` so the library path is covered too.
