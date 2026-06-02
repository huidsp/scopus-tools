import argparse
import contextlib
import datetime
import json
import logging
import os
import sys
from dotenv import load_dotenv
from scopus_tools import api, core, ai_engine, utils, kaken, linking, llm, scie


# どのサブコマンドにどの環境変数が必須か。
# eval は --kaken-id が指定された場合のみ KAKEN_APP_ID も必要(下のロジックで追加)。
# analyze / eval は --model に応じて OPENAI_API_KEY か ANTHROPIC_API_KEY を要求(動的判定)。
# webui は実行時に各機能ごとに鍵をチェックするため、ここでは何も必須にしない。
KEY_REQUIREMENTS = {
    "search":        ["SCOPUS_API_KEY"],
    "stats":         ["SCOPUS_API_KEY"],
    "summary":       ["SCOPUS_API_KEY"],
    "papers":        ["SCOPUS_API_KEY"],
    "batch":         ["SCOPUS_API_KEY"],
    "analyze":       ["SCOPUS_API_KEY"],
    "eval":          ["SCOPUS_API_KEY"],
    "kaken-search":  ["KAKEN_APP_ID"],
    "kaken-summary": ["KAKEN_APP_ID"],
    "webui":         [],
}

YEAR_RANGE_HELP = (
    "Year range. Accepted forms: 2021-2025, 2021,2025, 2021:2025, [2021,2025]"
)


def _parse_year_range(text, parser, default_years=5, announce=False):
    """年範囲文字列をパースして (start, end) のタプルを返す。

    受理する書式: '[2021,2025]', '2021,2025', '2021-2025', '2021:2025'。
    text が None なら前年を含む直近 default_years 年を返す
    (例: 2026 年中の実行 → (2021, 2025))。
    announce=True なら 1 行表示。
    """
    if text is None:
        start_y, end_y = core.default_eval_year_range(default_years=default_years)
        if announce:
            print(
                f"Using default year range: {start_y}-{end_y} "
                f"(use --years to override)",
                file=sys.stderr,
            )
        return (start_y, end_y)

    raw = text.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    for sep in (",", "-", ":"):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep)]
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                start_y, end_y = int(parts[0]), int(parts[1])
                if start_y > end_y:
                    parser.error(
                        f"--years: start year must be <= end year (got {start_y} > {end_y})"
                    )
                return (start_y, end_y)
            break
    parser.error(
        f"--years value '{text}' is invalid. {YEAR_RANGE_HELP}"
    )


def _emit_text(render, output_path):
    """テキスト出力。output_path 指定時はファイルにリダイレクト、無指定なら stdout。"""
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            with contextlib.redirect_stdout(f):
                render()
    else:
        render()


def _emit_json(payload, output_path):
    """JSON 出力(UTF-8、日本語そのまま、インデント 2)。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


def _collect_targets(args, parser):
    """positional `ids` または --input CSV から [(s_ids, label), ...] を返す。

    どちらも未指定なら parser.error、両方指定なら parser.error。
    """
    ids = getattr(args, "ids", None)
    input_path = getattr(args, "input", None)
    if ids and input_path:
        parser.error(f"{args.command}: cannot combine positional IDs with --input")
    if not ids and not input_path:
        parser.error(f"{args.command}: provide positional Scopus IDs or --input")
    if ids:
        return [(ids.split(","), None)]
    df = utils.read_input_csv(input_path, required_cols=["Scopus ID"])
    targets = []
    for _, row in df.iterrows():
        scopus_id_value = row.get("Scopus ID")
        if not scopus_id_value or str(scopus_id_value) == "nan":
            logging.warning("Missing Scopus ID for %s, skipping.", row.get("Name", ""))
            continue
        s_ids = [s.strip() for s in str(scopus_id_value).split(",") if s.strip()]
        if not s_ids:
            continue
        targets.append((s_ids, row.get("Name", "")))
    return targets


def _check_required_keys(parser, command, kaken_id=None, model=None):
    """サブコマンドが必要とする環境変数が揃っているかを確認し、無ければ即終了する。

    analyze / eval は model に応じて OPENAI_API_KEY か ANTHROPIC_API_KEY を要求(動的)。
    """
    required = list(KEY_REQUIREMENTS.get(command, []))
    if command in ("analyze", "eval") and model:
        try:
            required.append(llm.required_key_for(model))
        except ValueError:
            parser.error(f"{command}: unknown --model '{model}'. "
                         f"Supported: {', '.join(llm.SUPPORTED_MODELS)}")
    if command == "eval" and kaken_id:
        required.append("KAKEN_APP_ID")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        names = ", ".join(missing)
        parser.error(
            f"{command}: missing required environment variable(s): {names}. "
            f"Set them in .env or your shell environment."
        )


def main():
    load_dotenv()
    utils.setup_logging()

    parser = argparse.ArgumentParser(description="Scopus Data Retrieval & Analysis Tools")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # 1. search (旧 get_author.py)
    search_p = subparsers.add_parser("search", help="Search Scopus ID by name")
    search_p.add_argument("--name", help="Single author name")
    search_p.add_argument("--input", help="Input CSV with 'Name' column")
    search_p.add_argument("--output", help="Output CSV path")

    # 2. stats (旧 get_data.py)
    stats_p = subparsers.add_parser("stats", help="Get paper counts and citations for a year range")
    stats_p.add_argument("--years", "--year", dest="years", required=True, help=YEAR_RANGE_HELP)
    stats_p.add_argument("--input", required=True, help="Input CSV with 'Scopus ID'")
    stats_p.add_argument("--output", required=True, help="Output CSV path")

    # 3. summary (旧 scopus_summary.py)
    sum_p = subparsers.add_parser("summary", help="Show human-readable summary (H-index, Top 5 papers)")
    sum_p.add_argument("ids", nargs="?", help="Scopus IDs (comma separated)")
    sum_p.add_argument("--input", default=None,
                       help="Input CSV with 'Scopus ID' column (alternative to positional IDs)")
    sum_p.add_argument("--years", default=None, help=YEAR_RANGE_HELP + " (default: last 5 years)")
    sum_p.add_argument("--format", choices=["text", "json"], default="text",
                       help="Output format (default: text)")
    sum_p.add_argument("--output", default=None,
                       help="Write to file path instead of stdout")
    sum_p.add_argument("--scie-list", nargs="+", default=None, metavar="CSV",
                       help="One or more Web of Science journal lists (see `papers --scie-list`). "
                            "When given, the summary adds SCI(SCIE) paper counts and their "
                            "first-author counts for both the full and evaluation periods.")

    # 3b. papers (指定年範囲の論文一覧を取得)
    papers_p = subparsers.add_parser("papers", help="List papers published in a given year range")
    papers_p.add_argument("ids", nargs="?", help="Scopus IDs (comma separated)")
    papers_p.add_argument("--input", default=None,
                          help="Input CSV with 'Scopus ID' column (alternative to positional IDs)")
    papers_p.add_argument("--years", default=None, help=YEAR_RANGE_HELP + " (default: last 5 years)")
    papers_p.add_argument("--format", choices=["text", "json", "csv"], default="text",
                          help="Output format (default: text)")
    papers_p.add_argument("--output", default=None,
                          help="Write to file path instead of stdout (required for --format csv)")
    papers_p.add_argument("--scie-list", nargs="+", default=None, metavar="CSV",
                          help="One or more Web of Science journal lists (CSV with ISSN columns, or "
                               "one ISSN per line). The index name is derived from each filename's "
                               "parenthesized abbreviation, e.g. '... (SCIE).csv' -> SCIE. Each paper "
                               "is annotated with the matching index names (SCIE/SSCI/AHCI/...).")
    papers_p.add_argument("--scie-only", action="store_true",
                          help="Keep only papers indexed in at least one given list (requires --scie-list)")

    # 4. batch (旧 scopus_batch_summary.py)
    batch_p = subparsers.add_parser("batch", help="Batch generate summary CSV for multiple authors")
    batch_p.add_argument("--input", required=True, help="Input CSV")
    batch_p.add_argument("--output", required=True, help="Output CSV path")
    batch_p.add_argument("--years", default=None, help=YEAR_RANGE_HELP + " (default: last 5 years)")

    # 5. analyze (追加機能: AI連携 — OpenAI / Claude いずれか)
    analyze_p = subparsers.add_parser("analyze", help="AI-based expertise estimation")
    analyze_p.add_argument("ids", help="Scopus IDs")
    analyze_p.add_argument("--lang", default="ja", help="Output language")
    analyze_p.add_argument("--model", default=llm.DEFAULT_MODEL, choices=llm.SUPPORTED_MODELS,
                           help=f"AI model (default: {llm.DEFAULT_MODEL})")

    # 6. eval (業績に基づくAI総合評価)
    eval_p = subparsers.add_parser("eval", help="AI-based comprehensive evaluation of research achievements")
    eval_p.add_argument("ids", nargs="?", help="Scopus IDs (comma separated)")
    eval_p.add_argument("--input", default=None,
                        help="Input CSV with 'Scopus ID' column (alternative to positional IDs)")
    eval_p.add_argument("--years", default=None, help=YEAR_RANGE_HELP + " (default: last 5 years)")
    eval_p.add_argument("--lang", default="ja", help="Output language (default: ja)")
    eval_p.add_argument("--kaken-id", dest="kaken_id", default=None,
                        help="KAKEN researcher number(s), comma separated. "
                             "When given, KAKEN grant history is included in the AI evaluation.")
    eval_p.add_argument("--kaken-auto", dest="kaken_auto", action="store_true",
                        help="If KAKEN_APP_ID is set and --kaken-id is omitted, auto-link by author name "
                             "and take the first candidate without prompting on multiple matches.")
    eval_p.add_argument("--no-kaken", dest="no_kaken", action="store_true",
                        help="Disable KAKEN auto-linking even if KAKEN_APP_ID is set.")
    eval_p.add_argument("--format", choices=["text", "json"], default="text",
                        help="Output format (default: text)")
    eval_p.add_argument("--output", default=None,
                        help="Write to file path instead of stdout")
    eval_p.add_argument("--model", default=llm.DEFAULT_MODEL, choices=llm.SUPPORTED_MODELS,
                        help=f"AI model (default: {llm.DEFAULT_MODEL})")
    eval_p.add_argument("--paper-mode", dest="paper_mode",
                        choices=["first_author", "period_full"], default="first_author",
                        help="How paper details are fed to the AI: "
                             "'first_author' = top-10 cited papers with author position (default); "
                             "'period_full' = all in-range papers with author position")
    eval_p.add_argument("--scie-list", nargs="+", default=None, metavar="CSV",
                        help="One or more Web of Science journal lists (see `papers --scie-list`). "
                             "Index names are derived from filenames; per-index coverage counts are "
                             "included in the AI evaluation prompt.")

    # 7. kaken-search (科研費 KAKEN: 研究者検索)
    ks_p = subparsers.add_parser("kaken-search", help="Search KAKEN researcher by name or number")
    ks_p.add_argument("--name", help="Researcher full name (e.g., 'Victor Parque')")
    ks_p.add_argument("--id", dest="researcher_id", help="8-digit KAKEN researcher number")
    ks_p.add_argument("--lang", default="ja", help="Output language (default: ja)")

    # 8. kaken-summary (科研費獲得サマリー)
    ksum_p = subparsers.add_parser("kaken-summary", help="Show KAKEN grant summary for a researcher number")
    ksum_p.add_argument("ids", help="KAKEN researcher numbers (comma separated)")
    ksum_p.add_argument("--lang", default="ja", help="Output language (default: ja)")
    ksum_p.add_argument("--role", default=None,
                        help="Filter by role code, e.g. 'principal_investigator' "
                             "(see KAKEN API doc parameter c2)")
    ksum_p.add_argument("--format", choices=["text", "json"], default="text",
                        help="Output format (default: text)")
    ksum_p.add_argument("--output", default=None,
                        help="Write to file path instead of stdout")

    # 9. webui (Gradio による単一研究者ワークフロー)
    webui_p = subparsers.add_parser("webui", help="Launch the Gradio web UI (single-researcher workflow)")
    webui_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    webui_p.add_argument("--port", type=int, default=7860, help="Bind port (default: 7860)")
    webui_p.add_argument("--share", action="store_true", help="Create a Gradio public share link")
    webui_p.add_argument("--projects-dir", dest="projects_dir", default=None,
                         help="Directory to store project JSON files (default: ~/.scopus-tools/projects/)")
    webui_p.add_argument("--scie-list", nargs="+", default=None, metavar="CSV",
                         help="One or more Web of Science journal lists (see `papers --scie-list`). "
                              "Papers fetched in the UI are annotated with their index names "
                              "(SCIE/SSCI/...). If omitted, '*Citation Index*.csv' in the launch "
                              "directory is auto-loaded.")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    # サブコマンドが必要とする API キーを先行検証
    _check_required_keys(
        parser, args.command,
        kaken_id=getattr(args, "kaken_id", None),
        model=getattr(args, "model", None),
    )

    if args.command == "search":
        if args.name and (args.input or args.output):
            parser.error("search: --name cannot be combined with --input/--output")
        if not args.name and not (args.input and args.output):
            parser.error("search: provide either --name or both --input and --output")
        client = api.ScopusClient()
        if args.name:
            results = client.search_author_by_name(args.name)
            utils.print_author_results(args.name, results)
        else:
            utils.process_author_csv(args.input, args.output, client)

    elif args.command == "stats":
        client = api.ScopusClient()
        start_y, end_y = _parse_year_range(args.years, parser)
        df = utils.read_input_csv(args.input, required_cols=["Scopus ID"])
        results = []
        for _, row in df.iterrows():
            scopus_id_value = row.get("Scopus ID")
            if not scopus_id_value or str(scopus_id_value) == "nan":
                logging.warning("Missing Scopus ID for %s, skipping.", row.get("Name", ""))
                continue
            s_ids = str(scopus_id_value).split(",")
            paper_data = client.get_papers_by_year(s_ids, start_y, end_y)
            results.append({**row.to_dict(), **paper_data})
        utils.save_output_csv(results, args.output)

    elif args.command == "summary":
        client = api.ScopusClient()
        index_sets = scie.load_index_sets(args.scie_list) if args.scie_list else None
        targets = _collect_targets(args, parser)
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        is_batch = args.input is not None
        results = []
        for idx, (s_ids, label) in enumerate(targets, start=1):
            if is_batch:
                utils.progress(f"[{idx}/{len(targets)}] processing: {label or s_ids[0]}")
            papers = client.search_papers(s_ids)
            if index_sets is not None:
                scie.annotate_papers_indexes(papers, index_sets)
            first, last = client.get_author_profile(s_ids[0])
            report = core.summarize_papers(papers, year_range=year_range)
            results.append((s_ids, first, last, report, papers))
        if is_batch:
            utils.progress_done()

        if args.format == "json":
            items = [{
                "scopus_ids": s_ids,
                "author": {"first": first, "last": last},
                "year_range": list(year_range),
                "report": report,
                "papers": papers,
            } for (s_ids, first, last, report, papers) in results]
            payload = items if is_batch else items[0]
            _emit_json(payload, args.output)
        else:
            def _render_summary():
                for i, (s_ids, first, last, report, papers) in enumerate(results):
                    if i > 0:
                        print()
                    utils.print_report_text(first, last, s_ids, report, papers, year_range=year_range)
            _emit_text(_render_summary, args.output)

    elif args.command == "papers":
        if args.format == "csv" and not args.output:
            parser.error("papers: --format csv requires --output")
        if args.scie_only and not args.scie_list:
            parser.error("papers: --scie-only requires --scie-list")
        client = api.ScopusClient()
        index_sets = scie.load_index_sets(args.scie_list) if args.scie_list else None
        targets = _collect_targets(args, parser)
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        start_y, end_y = year_range
        query_extra = f"PUBYEAR > {start_y - 1} AND PUBYEAR < {end_y + 1}"
        is_batch = args.input is not None
        results = []
        for idx, (s_ids, label) in enumerate(targets, start=1):
            if is_batch:
                utils.progress(f"[{idx}/{len(targets)}] processing: {label or s_ids[0]}")
            papers = client.search_papers(s_ids, query_extra=query_extra)
            if index_sets is not None:
                scie.annotate_papers_indexes(papers, index_sets)
                if args.scie_only:
                    papers = [p for p in papers if p.get("wos_indexes")]
            first, last = client.get_author_profile(s_ids[0])
            results.append((s_ids, first, last, papers))
        if is_batch:
            utils.progress_done()

        if args.format == "json":
            items = [{
                "scopus_ids": s_ids,
                "author": {"first": first, "last": last},
                "year_range": list(year_range),
                "paper_count": len(papers),
                "papers": papers,
            } for (s_ids, first, last, papers) in results]
            payload = items if is_batch else items[0]
            _emit_json(payload, args.output)
        elif args.format == "csv":
            utils.save_papers_csv(results, args.output)
        else:
            def _render_papers():
                for i, (s_ids, first, last, papers) in enumerate(results):
                    if i > 0:
                        print()
                    utils.print_papers_list(first, last, s_ids, papers, year_range)
            _emit_text(_render_papers, args.output)

    elif args.command == "batch":
        client = api.ScopusClient()
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        utils.process_batch_summary(args.input, args.output, client, year_range=year_range)

    elif args.command == "analyze":
        client = api.ScopusClient()
        papers = client.search_papers(args.ids.split(","))
        print(ai_engine.estimate_expertise(papers, lang=args.lang, model=args.model))

    elif args.command == "eval":
        client = api.ScopusClient()
        index_sets = scie.load_index_sets(args.scie_list) if args.scie_list else None
        targets = _collect_targets(args, parser)
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        is_batch = args.input is not None
        explicit_kaken_ids = (
            [k.strip() for k in args.kaken_id.split(",") if k.strip()]
            if args.kaken_id else None
        )
        if explicit_kaken_ids and is_batch and len(targets) > 1:
            parser.error("eval: --kaken-id cannot be used with --input (KAKEN IDs are per-author)")

        results = []
        for idx, (s_ids, label) in enumerate(targets, start=1):
            if is_batch:
                utils.progress(f"[{idx}/{len(targets)}] processing: {label or s_ids[0]}")
            papers = client.search_papers(s_ids)
            if index_sets is not None:
                scie.annotate_papers_indexes(papers, index_sets)
            first, last = client.get_author_profile(s_ids[0])
            report = core.summarize_papers(papers, year_range=year_range)
            grants = None
            if explicit_kaken_ids is not None:
                resolved_kaken_ids = list(explicit_kaken_ids)
            elif not args.no_kaken and os.getenv("KAKEN_APP_ID"):
                try:
                    kaken_client = kaken.KakenClient()
                    resolved_kaken_ids = linking.resolve_kaken_researcher_ids(
                        first, last, kaken_client, lang=args.lang, auto=args.kaken_auto,
                    )
                except ValueError:
                    resolved_kaken_ids = []
            else:
                resolved_kaken_ids = []
            if resolved_kaken_ids:
                kaken_client = kaken.KakenClient()
                grants = []
                for kid in resolved_kaken_ids:
                    grants.extend(kaken_client.get_grants_by_researcher_id(kid, lang=args.lang))
            evaluation = ai_engine.evaluate_achievements(
                papers, report, lang=args.lang, grants=grants, model=args.model,
                paper_list_mode=args.paper_mode, year_range=year_range,
            )
            results.append((s_ids, first, last, report, resolved_kaken_ids, grants, evaluation))
        if is_batch:
            utils.progress_done()

        if args.format == "json":
            items = [{
                "scopus_ids": s_ids,
                "author": {"first": first, "last": last},
                "year_range": list(year_range),
                "report": report,
                "kaken_researcher_ids": kids,
                "kaken_grants_count": len(grants) if grants else 0,
                "kaken_grants": grants or [],
                "evaluation": evaluation,
            } for (s_ids, first, last, report, kids, grants, evaluation) in results]
            payload = items if is_batch else items[0]
            _emit_json(payload, args.output)
        else:
            def _render_eval():
                for i, (s_ids, first, last, report, kids, grants, evaluation) in enumerate(results):
                    if i > 0:
                        print()
                    print(f"=== 業績評価: {first} {last} ({', '.join(s_ids)}) ===")
                    print(f"評価期間: {year_range[0]}〜{year_range[1]}")
                    if kids:
                        print(
                            f"KAKEN研究者番号: {', '.join(kids)} "
                            f"({len(grants) if grants else 0} 課題取得)"
                        )
                    print()
                    print(evaluation)
            _emit_text(_render_eval, args.output)

    elif args.command == "kaken-search":
        if not args.researcher_id and not args.name:
            parser.error("kaken-search requires --name or --id")
        client = kaken.KakenClient()
        if args.researcher_id:
            r = client.search_researcher_by_id(args.researcher_id, lang=args.lang)
            utils.print_kaken_researcher_results(args.researcher_id, [r] if r else [])
        else:
            results = client.search_researcher_by_name(args.name, lang=args.lang)
            utils.print_kaken_researcher_results(args.name, results)

    elif args.command == "webui":
        try:
            from scopus_tools import webui
        except ImportError as e:
            parser.error(
                "webui: gradio is not installed. "
                "Install with: pip install -e \".[ui]\"  "
                f"(import error: {e})"
            )
        webui.launch(
            host=args.host, port=args.port, share=args.share,
            projects_dir=args.projects_dir, scie_list=args.scie_list,
        )
        return

    elif args.command == "kaken-summary":
        client = kaken.KakenClient()
        r_ids = [s.strip() for s in args.ids.split(",") if s.strip()]
        fetched = []
        for rid in r_ids:
            grants = client.get_grants_by_researcher_id(rid, lang=args.lang, role=args.role)
            researcher = client.search_researcher_by_id(rid, lang=args.lang)
            fetched.append((rid, researcher, grants))

        if args.format == "json":
            _emit_json({
                "researchers": [
                    {"researcher_id": rid, "researcher": researcher, "grants": grants}
                    for rid, researcher, grants in fetched
                ]
            }, args.output)
        else:
            def _render_kaken_summary():
                for rid, researcher, grants in fetched:
                    utils.print_kaken_summary(rid, researcher, grants)
            _emit_text(_render_kaken_summary, args.output)


if __name__ == "__main__":
    main()
