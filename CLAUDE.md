# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`scopus_tools` is a Python toolkit that combines the Elsevier Scopus REST API, the KAKEN (科研費) API,
and LLMs (OpenAI and Anthropic Claude) to fetch researcher data, compute bibliometric indicators
(H-index, G-index, citations, first-author counts), pull KAKEN grant histories, and produce
field-normalized AI evaluations. It ships:

- A CLI (`scopus-tools`) with 10 subcommands.
- A Gradio WebUI (`scopus-tools webui`) with a project-based hierarchical workspace
  (project → multiple researchers → Scopus / KAKEN / AI / comparison panels) that
  persists everything to JSON files.

The CLI is primarily Japanese-facing (report text and many log messages), but identifiers
and CLI flag names stay in English.

## Environment

- Python ≥ 3.9. Main dependencies: `requests`, `pandas`, `python-dotenv`, `openai`, `anthropic`.
  Optional extras: `[ui]` → `gradio>=4.0`; `[dev]` → `pytest`.
- A `.env` at the repo root is loaded by `cli.main` via `load_dotenv()`. Keys:
  - `SCOPUS_API_KEY` — required for any Scopus-touching command.
  - `OPENAI_API_KEY` — required when the chosen model is `gpt-*`.
  - `ANTHROPIC_API_KEY` — required when the chosen model is `claude-*` (the default).
  - `KAKEN_APP_ID` — required for KAKEN-touching commands.
- Local dev install:

  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -e ".[ui,dev]"
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

- `search` — Scopus ID lookup by name (single `--name` or batch `--input/--output` CSV).
- `stats` — paper / citation counts per year range; requires `--years`.
- `summary` — human-readable per-author report; supports `--input` CSV mode and `--format json`.
- `papers` — list papers published in a given `--years` range; positional IDs or `--input` CSV,
  `--format text|json|csv` (csv requires `--output`). Each paper carries `author_position` /
  `author_count` (shown as `2/3`, `1/4 (first)`) computed in `api.search_papers`.
  `--scie-list CSV [CSV ...]` annotates each paper with the matching Web of Science index
  names (SCIE/SSCI/AHCI/...) by ISSN match (see `scie`); `--scie-only` then keeps only papers
  indexed in at least one given list.
- `batch` — CSV-in / CSV-out summary across many authors.
- `analyze` — AI-based expertise estimation from paper titles; takes `--model`.
- `eval` — AI-based comprehensive field-normalized evaluation; supports `--kaken-id`,
  `--kaken-auto`, `--no-kaken`, `--input` CSV mode, `--format json`, `--model`.
  `--scie-list CSV [CSV ...]` adds per-index WoS coverage counts (total + in-period) to the
  evaluation prompt.
- `kaken-search` / `kaken-summary` — KAKEN researcher lookup and grant summary.
- `webui` — launches the Gradio WebUI on `127.0.0.1:7860`; supports `--projects-dir`,
  `--scie-list CSV [CSV ...]`, and `--scie-dir DIR` (loads every `*.csv` in DIR). With neither
  flag it auto-detects `*Citation Index*.csv` and `index/*.csv` in the launch dir. The Docker
  image mounts the index CSVs at `/data/index` (`docker-compose.yml` maps `./index:/data/index`).

Year-range arguments accept `2021-2025`, `2021,2025`, `2021:2025`, or `[2021,2025]`
(parsed by `_parse_year_range` in `cli.py`). When omitted, defaults to the **previous year inclusive,
last 5 years** via `core.default_eval_year_range()` (e.g., in 2026 → `(2021, 2025)`).

## Architecture

The codebase is intentionally flat — modules under `scopus_tools/`, each with a single responsibility.
Two control flows:

**CLI**: `cli.py` → `ScopusClient` / `KakenClient` / `ai_engine` → `core` (pure functions) → `utils` (presentation).
**WebUI**: `webui.py` → `ProjectStore` (JSON persistence) + same backends → Gradio components.

### Modules

- **`api.ScopusClient`** — the only Scopus network boundary.
  - `search_author_by_name` tries **both** `first last` and `last first` orderings and dedupes
    by Scopus ID. Workaround for ambiguous name parsing — don't "fix" it to a single pattern.
  - `search_papers` paginates with `view=COMPLETE`, dedupes by `eid`, takes `max(citations)`
    on duplicates, and ORs `is_first_author` flags (computed by matching the entry's first
    `authid` against the queried `author_ids` set).

- **`kaken.KakenClient`** — the only KAKEN network boundary. Wraps NRID (researcher) and KAKEN
  (project) OpenSearch endpoints. NRID returns JSON, KAKEN returns XML — both parsed into
  flat dicts. Requires `KAKEN_APP_ID`.

- **`core`** — pure functions, no I/O.
  - `compute_indices(citations)` → `(h, g)`.
  - `summarize_papers(papers, year_range)` returns a dict consumed by `utils.print_report_text`
    and `utils.process_batch_summary` — keep the keys in sync if you change either.
  - `resolve_year_range` (internal default) and `default_eval_year_range` (user-facing default
    for the UI/CLI: previous year inclusive, 5-year window).

- **`linking`** — Scopus author → KAKEN researcher number matching by name. Used by the CLI's
  `eval --kaken-auto` flow. The WebUI does its own selection through the KAKEN tab and
  doesn't rely on this module.

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
  - Lists are登録制 (Clarivate Master Journal List), one CSV per index, not auto-downloadable;
    pass them via `--scie-list`. Each index is a separate download — a single CSV has no
    per-row index label. The data files are gitignored.

- **`llm`** — provider abstraction over OpenAI and Anthropic.
  - `SUPPORTED_MODELS = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5", "gpt-5.4"]`,
    `DEFAULT_MODEL = "claude-opus-4-7"`.
  - `complete(model, prompt, *, json_mode=False, max_tokens=8192)` — non-streaming.
  - `stream(model, prompt, *, max_tokens=8192)` — generator yielding **cumulative** text
    (both providers normalized to this pattern).
  - `parse_json_response(text)` — robust JSON parser (strips ```code fences``` and preamble).
  - Provider is auto-detected from model name prefix (`gpt-*` / `claude-*`).

- **`ai_engine`** — high-level evaluation functions, all routing through `llm.*`.
  - `estimate_expertise(papers, lang, model)` — short expertise summary.
  - `infer_field(papers, model)` and `_infer_field_context(model, all_titles)` — field
    estimation in JSON mode (Anthropic uses prompt instruction + brace extraction).
  - `evaluate_achievements(papers, report, lang, grants, extra_instructions, model, paper_list_mode, year_range)` — full
    field-normalized evaluation. `paper_list_mode` selects how papers are fed to the prompt:
    `"first_author"` (default) = top-10 cited with author position `n/m`; `"period_full"` =
    all `year_range` papers with author position. Author position comes from `api.search_papers`
    (`author_position` / `author_count`); the prompt always asks the model to weigh research
    leadership (first vs co-author).
  - `evaluate_achievements_stream(..., field_ctx=None, ..., paper_list_mode, year_range)` — streaming version. Pass
    `field_ctx` to skip the inference call (WebUI caches it in `researcher.ai.field_ctx`).
    The WebUI AI tab exposes `paper_list_mode` as a radio (saved in `researcher.ai.paper_mode`).
  - `compare_researchers_stream(researchers_data, lang, extra_instructions, model)` —
    multi-candidate comparison with field normalization (used by the WebUI comparison tab).
  - `extra_instructions` is appended as `【評価の観点・追加指示】` to the prompt for
    user-driven prompt control.

- **`utils`** — CSV I/O (`utf-8-sig` output for Excel compatibility), logging setup,
  per-row progress helpers (`progress`, `progress_done`), and the two batch drivers
  (`process_author_csv`, `process_batch_summary`). Per-row error handling lives here —
  missing Scopus IDs are logged and skipped, not raised. Shared formatter helpers
  (`_hr`, `_section`) are used across the `print_*` functions.

- **`projects`** — JSON-file project store for the WebUI. **Hierarchical model**:
  - Project (a department / committee / cohort) contains a `researchers` list.
  - Each researcher has `scopus`, `kaken`, `ai` sections.
  - Project also has a top-level `comparison` section for cross-researcher analysis.
  - `ProjectStore` does CRUD with atomic writes (`tempfile + os.replace`).
  - `_migrate_if_legacy()` auto-converts old flat-format files (a single researcher per project)
    on load — don't remove this until you're sure no old files exist.

- **`cli`** — argparse dispatch only; no business logic.
  - `KEY_REQUIREMENTS` is a static map for non-AI subcommands; AI subcommands use
    `llm.required_key_for(model)` for dynamic dispatch in `_check_required_keys`.
  - `_collect_targets` centralizes positional `ids` vs `--input` CSV mode for `summary`/`eval`.

- **`webui`** — Gradio Blocks app.
  - Left sidebar: project Dropdown + researcher Radio + new/rename/delete (2-step delete confirm).
  - Right pane: 4 tabs (Scopus / KAKEN / AI / 比較).
  - All long state held in `gr.State` (`current_project_name`, `current_researcher_name`,
    `scopus_state`, `kaken_state`, etc.).
  - Auto-saves on each "実行" via `_save_researcher_section` / `set_project_comparison`.
  - Restore on project / researcher switch via `_researcher_to_updates` and
    `_compare_pane_from_project` (returns a dict that gets tupled in `_RIGHT_OUTPUT_KEYS` /
    `_COMPARE_OUTPUT_KEYS` order).
  - Copy / Export buttons use client-side JS (`_COPY_JS`, `_download_js`) — no temp files on
    the server (avoids a `gr.File` schema bug in `gradio_client` 1.3.x).
  - Streaming AI evaluation is a `yield from` generator hooked to `ai_run_btn.click`.
  - WoS index lists are loaded once at `launch(scie_list=..., scie_dir=...)` into the module
    global `_INDEX_SETS` (via `_load_index_sets`; precedence: explicit `scie_list` → all `*.csv`
    in `scie_dir` → auto-detect `*Citation Index*.csv` + `index/*.csv` in the launch dir).
    Docker passes `--scie-dir /data/index`. `_scopus_run` annotates fetched papers with
    `wos_indexes`, so the papers list (`print_papers_list`) and AI eval (`_build_eval_prompt`)
    surface SCIE/SSCI/... automatically. The env banner reports which indexes were loaded.

## Tests

`tests/` uses `pytest` and mocks at the right boundaries:

- `tests/test_scopus_tools.py` — primary suite. Mocks `ScopusClient` at the `requests.get`
  level, mocks `llm.complete` / `llm.stream` for AI functions, uses `DUMMY_PAPERS` fixture.
- `tests/test_llm.py` — provider abstraction unit tests. Mocks `openai.OpenAI` and
  `anthropic.Anthropic` directly. No live API calls.
- `tests/test_ai_engine_compare.py` — comparison and extra-instructions tests.
- `tests/test_projects.py` — `ProjectStore` CRUD + legacy migration tests.
- `tests/test_legacy_compat.py` — guards backward-compatible behavior against the
  pre-refactor scripts in `old/` (gitignored).

When you change the default model in `llm.DEFAULT_MODEL`, the test fixture `_DUMMY_ENV` in
`tests/test_scopus_tools.py` must include the matching env key, and any test asserting the
default's identity should use `llm.DEFAULT_MODEL` / `llm.required_key_for(llm.DEFAULT_MODEL)`
instead of hard-coding.

## Notes

- `old/` contains pre-refactor standalone scripts and is gitignored. Reference only; don't import.
- Output CSVs use `utf-8-sig` so Excel renders Japanese correctly — don't switch to plain
  `utf-8` without reason.
- Log progress messages in `search_papers` are intentionally in Japanese ("Scopus取得進捗…");
  the report text in `utils.print_report_text` is also Japanese. UI strings are not internationalized.
- The WebUI's `_check_keys()` returns a 5-tuple `(scopus_ok, ai_ok, kaken_ok, openai_ok, anthropic_ok)`
  where `ai_ok` is the OR of `openai_ok` / `anthropic_ok`. Adjust callers accordingly if you
  add another provider.
- Anthropic's `client.messages.stream(...)` requires `max_tokens` — `llm.stream` defaults to 8192
  but the comparison flow uses 16384 for longer multi-candidate outputs.
- Anthropic has no native JSON mode; `llm.complete(..., json_mode=True)` appends an instruction
  in the prompt and `parse_json_response` falls back to brace extraction.
