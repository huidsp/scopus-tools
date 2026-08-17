import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
from dotenv import find_dotenv, load_dotenv
from scopus_tools import api, asof, cachedb, config, core, httpcache, mcp_setup, utils, kaken, scie


# どのサブコマンドにどの環境変数が必須か。
# mcp は起動時ではなくツール呼び出し時に鍵を判定するため、ここでは何も必須にしない。
KEY_REQUIREMENTS = {
    "search":        ["SCOPUS_API_KEY"],
    "stats":         ["SCOPUS_API_KEY"],
    "summary":       ["SCOPUS_API_KEY"],
    "papers":        ["SCOPUS_API_KEY"],
    "batch":         ["SCOPUS_API_KEY"],
    "find":          ["SCOPUS_API_KEY"],
    "kaken-search":  ["KAKEN_APP_ID"],
    "kaken-summary": ["KAKEN_APP_ID"],
    "mcp":           [],
    "mcp-setup":     [],
    "config":        [],
    "cache":         [],
}

YEAR_RANGE_HELP = "Year range. " + core.YEAR_RANGE_HELP


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

    try:
        return core.parse_year_range(text, default_years=default_years)
    except ValueError as e:
        parser.error(f"--years: {e}")


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


def _is_blank_id(value):
    """CSV の Scopus ID セルが実質空かどうか。

    `"nan"` は pandas 時代の空セル表現。標準ライブラリの csv では空文字になるが、
    過去に書き出した CSV に文字列 "nan" が残っている可能性があるので両方弾く。
    """
    return not value or str(value).strip().lower() in ("", "nan")


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
    rows = utils.read_input_csv(input_path, required_cols=["Scopus ID"])
    targets = []
    for row in rows:
        scopus_id_value = row.get("Scopus ID")
        if _is_blank_id(scopus_id_value):
            logging.warning("Missing Scopus ID for %s, skipping.", row.get("Name", ""))
            continue
        s_ids = [s.strip() for s in str(scopus_id_value).split(",") if s.strip()]
        if not s_ids:
            continue
        targets.append((s_ids, row.get("Name", "")))
    return targets


def _check_required_keys(parser, command):
    """サブコマンドが必要とする環境変数が揃っているかを確認し、無ければ即終了する。"""
    required = list(KEY_REQUIREMENTS.get(command, []))
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        names = ", ".join(missing)
        parser.error(
            f"{command}: missing required environment variable(s): {names}.\n"
            f"Put them in {user_env_path()} (works from any directory), "
            f"or in a .env in the current directory, or export them in your shell.\n"
            f"  mkdir -p {cachedb.default_state_dir()}\n"
            f"  printf '{names.split(', ')[0]}=your_key\\n' >> {user_env_path()}\n"
            f"  chmod 600 {user_env_path()}"
        )


def _load_env_files():
    """`.env` を読み込む。先に読まれた値が優先される
    (`load_dotenv` は既存の環境変数を上書きしないため)。

    探索順:
      1. 実行時のカレントディレクトリから上へ — プロジェクトごとに鍵を分けたい場合
      2. `~/.scopus-tools/.env` — **どこから実行しても効く正規の置き場所**。
         `uv tool install` で入れた場合はリポジトリのクローンが手元に無いので、
         ここが唯一の安定した置き場所になる(キャッシュ DB とプロジェクト JSON も
         同じディレクトリに住んでいる)
      3. パッケージの位置から上へ — editable インストールでリポジトリ直下の
         `.env` を拾うため。実インストールでは site-packages を遡るので当たらない
    """
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(user_env_path())
    load_dotenv()


def user_env_path():
    """どこから実行しても読まれる `.env` のパス(実体は `config.user_env_path`)。"""
    return config.user_env_path()


def main():
    _load_env_files()
    utils.setup_logging()

    # キャッシュ / ネットワークのグローバル設定。
    # サブコマンドの前後どちらに書いても効くよう、親パーサとして全サブコマンドにも
    # 継承させる(MCP クライアントの設定では `mcp --cache-db ...` と後ろに書くのが自然)。
    # 継承側の既定値で前置き指定を上書きしないよう argparse.SUPPRESS を使う。
    common = argparse.ArgumentParser(add_help=False)
    cache_group = common.add_argument_group("cache and network")
    cache_group.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS,
                             help="Bypass the cache and refetch, overwriting cached responses")
    cache_group.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                             help="Serve only from cache; error instead of making a request")
    cache_group.add_argument("--no-cache", dest="no_cache", action="store_true",
                             default=argparse.SUPPRESS,
                             help="Neither read nor write the cache")
    cache_group.add_argument("--cache-db", dest="cache_db", metavar="PATH",
                             default=argparse.SUPPRESS,
                             help="Cache database path "
                                  "(default: $SCOPUS_TOOLS_CACHE_DB or ~/.scopus-tools/cache.sqlite3)")
    cache_group.add_argument("--timeout", type=float, metavar="SEC", default=argparse.SUPPRESS,
                             help="Per-request timeout in seconds (default: 10s connect / 60s read)")
    cache_group.add_argument("--stale-days", dest="stale_days", type=int, metavar="N",
                             default=argparse.SUPPRESS,
                             help="Warn when cached data is older than N days (default: per API — "
                                  "scopus_search 30, author lookups 90, kaken_project 30). "
                                  "Nothing is auto-refetched; this only controls the warning.")
    cache_group.add_argument("--stale-days-for", dest="stale_days_for", action="append",
                             metavar="API=N", default=argparse.SUPPRESS,
                             help="Per-API freshness threshold, repeatable "
                                  "(e.g. --stale-days-for scopus_search=14)")

    parser = argparse.ArgumentParser(
        description="Scopus Data Retrieval & Analysis Tools", parents=[common])
    _subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    class _Subcommands:
        """全サブコマンドに共通フラグ(`common`)を継承させる薄いラッパ。"""

        def add_parser(self, name, **kwargs):
            return _subparsers.add_parser(name, parents=[common], **kwargs)

    subparsers = _Subcommands()

    # 1. search (旧 get_author.py)
    search_p = subparsers.add_parser("search", help="Search Scopus ID by name")
    search_p.add_argument("--name", help="Single author name, given name first "
                                        "(e.g. 'Hiroyuki Okamura'). Split on whitespace; "
                                        "use --first/--last to be explicit.")
    search_p.add_argument("--first", dest="first_name", default=None,
                          help="Given name (use with --last; costs one API request)")
    search_p.add_argument("--last", dest="last_name", default=None,
                          help="Surname (use with --first)")
    search_p.add_argument("--try-both", action="store_true",
                          help="Also try the reversed name order. Doubles Author Search "
                               "quota usage (5,000/week) — only when the order is unknown.")
    search_p.add_argument("--input", help="Input CSV with 'Name' column, or "
                                          "'First Name'/'Last Name' columns")
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

    # 4b. find (タイトル / DOI から論文を引き、著者 ID を得る)
    find_p = subparsers.add_parser(
        "find", help="Find papers by title or DOI, with author Scopus IDs")
    find_p.add_argument("--title", default=None, help="Paper title (partial titles work)")
    find_p.add_argument("--doi", default=None, help="DOI (most reliable)")
    find_p.add_argument("--last", dest="find_last_name", default=None,
                        help="Author surname, to narrow an ambiguous title")
    find_p.add_argument("--limit", type=int, default=10,
                        help="Max papers to show (default: 10)")
    find_p.add_argument("--abstract", dest="include_abstract", action="store_true",
                        help="Include abstracts (long)")
    find_p.add_argument("--format", choices=["text", "json"], default="text",
                        help="Output format (default: text)")
    find_p.add_argument("--output", default=None,
                        help="Write to file path instead of stdout")

    # 5. kaken-search (科研費 KAKEN: 研究者検索)
    ks_p = subparsers.add_parser("kaken-search", help="Search KAKEN researcher by name or number")
    ks_p.add_argument("--name", help="Researcher full name (e.g., 'Victor Parque')")
    ks_p.add_argument("--id", dest="researcher_id", help="8-digit KAKEN researcher number")
    ks_p.add_argument("--lang", default="ja", help="Output language (default: ja)")

    # 6. kaken-summary (科研費獲得サマリー)
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

    # 7. mcp (MCP サーバ / stdio。データ取得ツールのみを公開)
    mcp_p = subparsers.add_parser(
        "mcp",
        help="Run as an MCP server over stdio (exposes the data-retrieval tools only)")
    mcp_p.add_argument("--projects-dir", dest="projects_dir", default=None,
                       help="Directory to store project JSON files "
                            "(default: ~/.scopus-tools/projects/)")
    mcp_p.add_argument("--scie-list", nargs="+", default=None, metavar="CSV",
                       help="One or more Web of Science journal lists (see `papers --scie-list`).")
    mcp_p.add_argument("--scie-dir", dest="scie_dir", default=None, metavar="DIR",
                       help="Directory holding the Web of Science index CSVs; every '*.csv' in it "
                            "is loaded. Used by the Docker image (mount to /data/index). If neither "
                            "--scie-list nor --scie-dir is given, '*Citation Index*.csv' and "
                            "'index/*.csv' in the launch directory are auto-loaded.")

    # 8. mcp-setup (MCP クライアントへの登録)
    setup_p = subparsers.add_parser(
        "mcp-setup",
        help="Register this tool as an MCP server with Claude Code / Claude Desktop")
    target = setup_p.add_mutually_exclusive_group()
    target.add_argument("--claude-code", dest="claude_code", action="store_true",
                        help="Register with Claude Code via `claude mcp add` (default)")
    target.add_argument("--claude-desktop", dest="claude_desktop", action="store_true",
                        help="Write into claude_desktop_config.json")
    setup_p.add_argument("--name", default=mcp_setup.DEFAULT_SERVER_NAME,
                         help=f"Server name (default: {mcp_setup.DEFAULT_SERVER_NAME})")
    setup_p.add_argument("--scope", choices=["local", "user", "project"], default=None,
                         help="Claude Code scope. Use 'user' to make it available everywhere.")
    setup_p.add_argument("--print", dest="print_only", action="store_true",
                         help="Show what would be registered, write nothing")
    setup_p.add_argument("--status", action="store_true", help="Show current registration")
    setup_p.add_argument("--remove", action="store_true", help="Remove the registration")
    setup_p.add_argument("--no-keys", dest="no_keys", action="store_true",
                         help="Do not embed API keys in the registration "
                              "(the server will then need them from its own environment)")
    setup_p.add_argument("--allow-protected-paths", dest="allow_protected_paths",
                         action="store_true",
                         help="Register even if a path is inside ~/Documents, ~/Desktop or "
                              "~/Downloads (macOS TCC). A GUI client will fail to start it.")
    setup_p.add_argument("--fix-permissions", dest="fix_permissions", action="store_true",
                         help="chmod 600 the .env file if it is readable by others")
    setup_p.add_argument("--scie-dir", dest="setup_scie_dir", default=None, metavar="DIR",
                         help="Passed through to `mcp --scie-dir`")
    setup_p.add_argument("--scie-list", dest="setup_scie_list", nargs="+", default=None,
                         metavar="CSV", help="Passed through to `mcp --scie-list`")
    setup_p.add_argument("--projects-dir", dest="setup_projects_dir", default=None,
                         help="Passed through to `mcp --projects-dir`")

    # 8b. config (API キーの保存)
    config_p = subparsers.add_parser(
        "config", help="Store API keys in ~/.scopus-tools/.env")
    config_p.add_argument("assignments", nargs="*", metavar="KEY=VALUE",
                          help="e.g. SCOPUS_API_KEY=xxxx. Omit the value "
                               "(just SCOPUS_API_KEY) to be prompted without echo, "
                               "which keeps the key out of your shell history.")
    config_p.add_argument("--unset", nargs="+", default=None, metavar="KEY",
                          help="Remove keys")
    config_p.add_argument("--path", dest="show_path", action="store_true",
                          help="Print the .env path and exit")

    # 9. cache (キャッシュの運用)
    cache_p = subparsers.add_parser("cache", help="Inspect and maintain the response cache")
    cache_sub = cache_p.add_subparsers(dest="cache_command")
    cache_sub.add_parser("stats", help="Show entry counts, size and quota state")
    cache_sub.add_parser("path", help="Print the cache database path")
    cache_sub.add_parser("vacuum", help="Compact the cache database")
    cl_p = cache_sub.add_parser("list", help="List cached entries")
    cl_p.add_argument("--api", default=None, help="Filter by API family")
    cl_p.add_argument("--older-than", dest="older_than", type=float, default=None,
                      metavar="DAYS", help="Only entries older than DAYS")
    cl_p.add_argument("--limit", type=int, default=50, help="Max rows (default: 50)")
    cc_p = cache_sub.add_parser("clear", help="Delete cached entries")
    cc_p.add_argument("--api", default=None, help="Only this API family")
    cc_p.add_argument("--older-than", dest="older_than", type=float, default=None,
                      metavar="DAYS", help="Only entries older than DAYS")
    cc_p.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    args = parser.parse_args()

    # 共通フラグは argparse.SUPPRESS なので、指定が無ければ属性自体が無い。
    # (サブコマンド側の既定値がサブコマンド前の指定を上書きしないようにするため)
    for name, default in (("refresh", False), ("offline", False), ("no_cache", False),
                          ("cache_db", None), ("timeout", None),
                          ("stale_days", None), ("stale_days_for", None)):
        if not hasattr(args, name):
            setattr(args, name, default)

    if args.command is None:
        parser.print_help()
        return

    # サブコマンドが必要とする API キーを先行検証
    _check_required_keys(parser, args.command)

    try:
        stale_overrides = asof.parse_overrides(args.stale_days_for)
    except ValueError as e:
        parser.error(str(e))
    args.stale_policy = asof.StalePolicy(overrides=stale_overrides, default=args.stale_days)

    # cache / mcp-setup はネットワークを使わないので Session を作らない
    if args.command == "cache":
        _cache_command(args, parser)
        return
    if args.command == "mcp-setup":
        _mcp_setup_command(args, parser)
        return
    if args.command == "config":
        _config_command(args, parser)
        return

    # キャッシュ DB と Session はプロセスで 1 つだけ作り、全クライアントで共有する
    http_ctx = httpcache.build_context(
        cache_db=args.cache_db, no_cache=args.no_cache,
        refresh=args.refresh, offline=args.offline, timeout=args.timeout)
    try:
        _dispatch(args, parser, http_ctx)
    except httpcache.OfflineError as e:
        parser.error(str(e))
    except httpcache.QuotaExceeded as e:
        parser.error(str(e))
    finally:
        http_ctx.close()


def _mask_keys(cmd):
    """表示用にコマンド列の API キー値を伏せる(端末やスクロールバックに残さない)。"""
    masked = []
    for arg in cmd:
        name, sep, _value = arg.partition("=")
        if sep and name in mcp_setup.KEY_NAMES:
            masked.append(f"{name}=***")
        else:
            masked.append(arg)
    return masked


def _config_command(args, parser):
    """`scopus-tools config [KEY=VALUE ...]`。API キーを ~/.scopus-tools/.env に保存する。"""
    import getpass

    if args.show_path:
        print(config.user_env_path())
        return

    if args.unset:
        removed = config.unset_keys(args.unset)
        print(f"Removed: {', '.join(removed)}" if removed else "Nothing to remove.")
        return

    if args.assignments:
        try:
            assignments, need_value = config.parse_assignments(args.assignments)
        except ValueError as e:
            parser.error(f"config: {e}")
        # 値を省略されたものは echo せずに入力させる(シェル履歴に残さないため)
        for key in need_value:
            try:
                value = getpass.getpass(f"{key}: ")
            except (EOFError, KeyboardInterrupt):
                print()
                parser.error("config: aborted")
            if not value.strip():
                parser.error(f"config: no value given for {key}")
            assignments[key] = value.strip()
        try:
            path = config.set_keys(assignments)
        except ValueError as e:
            parser.error(f"config: {e}")
        print(f"Saved {', '.join(sorted(assignments))} to {path} (mode 600)")
        return

    # 引数が無ければ現状を表示する
    info = config.describe()
    print(f"Key file: {info['path']}")
    if not info["exists"]:
        print("  (not created yet)")
        print("\nSet a key with either of:")
        print("  scopus-tools config SCOPUS_API_KEY=your_key")
        print("  scopus-tools config SCOPUS_API_KEY      # prompts, stays out of shell history")
        return
    print(f"  mode: {info['mode']}")
    for key, masked in info["keys"].items():
        print(f"  {key} = {masked}")
    for key in info["missing"]:
        print(f"  {key} = (not set)")
    if info["other_keys"]:
        print(f"  other keys kept as-is: {', '.join(info['other_keys'])}")
    if info["world_readable"]:
        print(f"\nWARNING: {info['path']} is readable by other users. "
              f"Run: chmod 600 {info['path']}")


def _mcp_setup_command(args, parser):
    """`scopus-tools mcp-setup ...`。MCP クライアントへの登録を代行する。"""
    use_desktop = args.claude_desktop
    entry = mcp_setup.build_entry(
        scie_dir=args.setup_scie_dir, scie_list=args.setup_scie_list,
        projects_dir=args.setup_projects_dir, cache_db=args.cache_db,
        with_keys=not args.no_keys)

    if args.status:
        if use_desktop:
            st = mcp_setup.desktop_status(args.name)
            print(f"Claude Desktop: {st['path']}")
            print(f"  registered: {st['registered']}")
            if st.get("entry"):
                print(f"  command   : {st['entry'].get('command')}")
                print(f"  args      : {' '.join(st['entry'].get('args') or [])}")
                print(f"  env keys  : {', '.join((st['entry'].get('env') or {}).keys()) or '(none)'}")
            if st.get("error"):
                print(f"  error     : {st['error']}")
        else:
            print("Claude Code registrations (`claude mcp list`):")
            sys.stdout.flush()
            subprocess.run(["claude", "mcp", "list"])
        return

    if args.remove:
        try:
            if use_desktop:
                res = mcp_setup.unregister_claude_desktop(args.name)
                print(f"{'Removed' if res['removed'] else 'Not registered'}: {res['path']}")
            else:
                res = mcp_setup.remove_claude_code(args.name, scope=args.scope)
                print(res["output"] or ("Removed" if res["ok"] else "Not registered"))
        except RuntimeError as e:
            parser.error(str(e))
        return

    if args.print_only:
        if use_desktop:
            preview = dict(entry)
            if preview.get("env"):
                # プレビューなので鍵は伏せる(実際の書き込み時は本物が入る)
                preview["env"] = {k: "***" for k in preview["env"]}
            print(f"Would write to {mcp_setup.claude_desktop_config_path()}:")
            print(json.dumps({"mcpServers": {args.name: preview}},
                             ensure_ascii=False, indent=2))
        else:
            cmd = mcp_setup.claude_code_command(entry, name=args.name, scope=args.scope)
            print(" ".join(_mask_keys(cmd)))
        return

    # TCC 保護配下のパスを登録すると Claude Desktop からは起動すらできない。
    # (Claude Code はターミナル経由で権限があるため通ってしまい、気付きにくい)
    problems = mcp_setup.tcc_warnings(entry)
    if problems:
        print("ERROR: these paths are inside a macOS privacy-protected folder, so a GUI "
              "client like Claude Desktop cannot read them:", file=sys.stderr)
        for path, folder, what in problems:
            print(f"  {what}: {path}   (~/{folder} is TCC-protected)", file=sys.stderr)
        print("\nThe server would fail to start with a PermissionError before it could "
              "even report anything. Install and point at paths outside ~/Documents, "
              "~/Desktop and ~/Downloads:", file=sys.stderr)
        print('  uv tool install "scopus_tools[mcp] @ /path/to/repo"', file=sys.stderr)
        print("  mkdir -p ~/.scopus-tools/index && cp /path/to/repo/index/*.csv "
              "~/.scopus-tools/index/", file=sys.stderr)
        print("  scopus-tools mcp-setup --claude-desktop --scie-dir ~/.scopus-tools/index",
              file=sys.stderr)
        print("\nPass --allow-protected-paths to register anyway.", file=sys.stderr)
        if not args.allow_protected_paths:
            parser.error("mcp-setup: refusing to register a path a GUI client cannot read")

    keys = {} if args.no_keys else mcp_setup.collect_keys()
    try:
        if use_desktop:
            res = mcp_setup.register_claude_desktop(entry, name=args.name)
            action = "Updated" if res["replaced"] else "Registered"
            print(f"{action} MCP server '{args.name}' in {res['path']}")
            if res["backup"]:
                print(f"  backup: {res['backup']}")
        else:
            res = mcp_setup.run_claude_code(entry, name=args.name, scope=args.scope)
            action = "Updated" if res.get("replaced") else "Registered"
            print(f"{action} MCP server '{args.name}' with Claude Code"
                  f"{f' (scope: {args.scope})' if args.scope else ''}")
            if res["output"]:
                print(f"  {res['output']}")
    except RuntimeError as e:
        parser.error(str(e))

    print(f"  command: {entry['command']}")
    print(f"  args   : {' '.join(entry['args'])}")

    # 鍵を書き込んだ事実は必ず明示する(平文で残るため)
    if keys:
        where = res.get("path") if use_desktop else "the Claude Code configuration"
        print(f"  NOTE: {', '.join(keys)} was written in plain text to {where}. "
              f"Use --no-keys to skip this.")
    else:
        print("  NOTE: no API keys were embedded. The MCP client does not inherit your "
              "shell environment, so tool calls will fail until the keys are supplied "
              "(re-run without --no-keys, or set them in the client's own config).")

    env_check = mcp_setup.check_env_permissions(fix=args.fix_permissions)
    if env_check and env_check["world_readable"]:
        if env_check["fixed"]:
            print(f"  Fixed permissions on {env_check['path']} → {env_check['mode']}")
        else:
            print(f"  WARNING: {env_check['path']} is {env_check['mode']} "
                  f"(readable by other users). Run `chmod 600 {env_check['path']}` "
                  f"or pass --fix-permissions.")


def _cache_command(args, parser):
    """`scopus-tools cache ...`。ネットワークには触れない。"""
    db = cachedb.CacheDB(args.cache_db)
    sub = getattr(args, "cache_command", None)
    if sub in (None, "stats"):
        s = db.stats()
        print(f"Cache: {s['path']}")
        print(f"  entries : {s['entries']}")
        print(f"  size    : {s['body_bytes'] / 1024:.1f} KiB")
        print(f"  hits    : {s['hits']}")
        if s["oldest_ts"]:
            print(f"  oldest  : {_ts(s['oldest_ts'])}")
            print(f"  newest  : {_ts(s['newest_ts'])}")
        if s["per_api"]:
            print("  by API:")
            for r in s["per_api"]:
                days = args.stale_policy.days_for(r["api"])
                print(f"    {r['api']:<26} {r['n']:>5} entries, {r['hits']:>5} hits, "
                      f"oldest {_ts(r['oldest_ts'])} (stale after {days}d)")
        if s["rate_limits"]:
            print("  quota:")
            for r in s["rate_limits"]:
                state = "BLOCKED" if r["quota_blocked"] else "ok"
                rem = "?" if r["remaining"] is None else r["remaining"]
                lim = "?" if r["limit_total"] is None else r["limit_total"]
                extra = f", resets {_ts(r['reset_ts'])}" if r["reset_ts"] else ""
                print(f"    {r['api']:<26} {rem}/{lim} remaining [{state}]{extra}")
    elif sub == "path":
        print(db.path)
    elif sub == "vacuum":
        db.vacuum()
        print("Vacuumed.")
    elif sub == "list":
        rows = db.list_entries(api=args.api, older_than_days=args.older_than, limit=args.limit)
        if not rows:
            print("No matching cache entries.")
        for r in rows:
            print(f"{r['fetched_at']}  {r['api']:<26} {r['size']:>8}B  hits={r['hits']:<4} "
                  f"{r['params_json'][:80]}")
    elif sub == "clear":
        if not args.yes:
            scope = args.api or "all APIs"
            age = f" older than {args.older_than} days" if args.older_than else ""
            reply = input(f"Delete cached entries for {scope}{age}? [y/N] ")
            if reply.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return
        n = db.prune(api=args.api, older_than_days=args.older_than)
        print(f"Deleted {n} cache entries.")
    else:
        parser.error(f"cache: unknown subcommand '{sub}'")
    db.close()


def _asof_entry(label, records):
    """`HttpLayer.collect()` の記録から、as-of 判定用の 1 エントリを作る。

    その操作の as-of は、構成したリクエストの**最も古い取得日時**。
    """
    known = [r for r in (records or []) if r.get("fetched_at")]
    oldest = min((r["fetched_at"] for r in known), default=None)
    return {"label": label, "fetched_at": oldest}


def _print_asof(entries, policy):
    """取得日のばらつきを stderr に警告する(自動再取得はしない)。"""
    if len(entries) < 2:
        return
    asof.print_asof_footer(asof.spread(entries))


def _ts(epoch):
    import datetime as _dt
    if not epoch:
        return "-"
    return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _dispatch(args, parser, http_ctx):
    """サブコマンドの実処理。HTTP コンテキストは呼び出し側が閉じる。"""

    if args.command == "search":
        explicit = args.first_name or args.last_name
        if explicit and args.name:
            parser.error("search: --name cannot be combined with --first/--last")
        if explicit and not (args.first_name and args.last_name):
            parser.error("search: --first and --last must be given together")
        if (args.name or explicit) and (args.input or args.output):
            parser.error("search: --name/--first/--last cannot be combined with --input/--output")
        if not args.name and not explicit and not (args.input and args.output):
            parser.error("search: provide --name, --first/--last, or both --input and --output")
        client = api.ScopusClient(context=http_ctx)
        if explicit:
            results = client.search_author(args.first_name, args.last_name)
            utils.print_author_results(f"{args.first_name} {args.last_name}", results)
        elif args.name:
            first, last = client.split_name(args.name)
            if not first:
                parser.error(f"search: cannot split '{args.name}' into given name and surname. "
                             f"Use --first/--last.")
            if not args.try_both:
                print(f"Assuming given name='{first}', surname='{last}' "
                      f"(use --first/--last to be explicit, or --try-both to try both orders)",
                      file=sys.stderr)
            results = client.search_author_by_name(args.name, try_both=args.try_both)
            utils.print_author_results(args.name, results)
        else:
            utils.process_author_csv(args.input, args.output, client,
                                     try_both=args.try_both)

    elif args.command == "stats":
        client = api.ScopusClient(context=http_ctx)
        start_y, end_y = _parse_year_range(args.years, parser)
        rows = utils.read_input_csv(args.input, required_cols=["Scopus ID"])
        results = []
        for row in rows:
            scopus_id_value = row.get("Scopus ID")
            if _is_blank_id(scopus_id_value):
                logging.warning("Missing Scopus ID for %s, skipping.", row.get("Name", ""))
                continue
            s_ids = str(scopus_id_value).split(",")
            paper_data = client.get_papers_by_year(s_ids, start_y, end_y)
            results.append({**row, **paper_data})
        utils.save_output_csv(results, args.output)

    elif args.command == "summary":
        client = api.ScopusClient(context=http_ctx)
        index_sets = scie.load_index_sets(args.scie_list) if args.scie_list else None
        targets = _collect_targets(args, parser)
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        is_batch = args.input is not None
        results = []
        asof_entries = []
        for idx, (s_ids, label) in enumerate(targets, start=1):
            if is_batch:
                utils.progress(f"[{idx}/{len(targets)}] processing: {label or s_ids[0]}")
            with client._http.collect() as records:
                fetched = client.search_papers_detailed(s_ids)
                first, last = client.get_author_profile(s_ids[0])
            papers = fetched.papers
            if not fetched.complete:
                print(f"WARNING: incomplete publication list for "
                      f"{label or ','.join(s_ids)} ({fetched.reason}). "
                      f"H-index and citation totals below are understated.", file=sys.stderr)
            asof_entries.append(_asof_entry(label or f"{first} {last}".strip() or s_ids[0],
                                            records))
            if index_sets is not None:
                scie.annotate_papers_indexes(papers, index_sets)
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

        # 取得日のばらつきは stderr に(--format json の stdout を汚さない)
        _print_asof(asof_entries, args.stale_policy)

    elif args.command == "papers":
        if args.format == "csv" and not args.output:
            parser.error("papers: --format csv requires --output")
        if args.scie_only and not args.scie_list:
            parser.error("papers: --scie-only requires --scie-list")
        client = api.ScopusClient(context=http_ctx)
        index_sets = scie.load_index_sets(args.scie_list) if args.scie_list else None
        targets = _collect_targets(args, parser)
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        start_y, end_y = year_range
        query_extra = f"PUBYEAR > {start_y - 1} AND PUBYEAR < {end_y + 1}"
        is_batch = args.input is not None
        results = []
        asof_entries = []
        for idx, (s_ids, label) in enumerate(targets, start=1):
            if is_batch:
                utils.progress(f"[{idx}/{len(targets)}] processing: {label or s_ids[0]}")
            with client._http.collect() as records:
                fetched = client.search_papers_detailed(s_ids, query_extra=query_extra)
                first, last = client.get_author_profile(s_ids[0])
            papers = fetched.papers
            if not fetched.complete:
                print(f"WARNING: incomplete publication list for "
                      f"{label or ','.join(s_ids)} ({fetched.reason}). "
                      f"Do not treat these counts as complete.", file=sys.stderr)
            asof_entries.append(_asof_entry(label or f"{first} {last}".strip() or s_ids[0],
                                            records))
            if index_sets is not None:
                scie.annotate_papers_indexes(papers, index_sets)
                if args.scie_only:
                    papers = [p for p in papers if p.get("wos_indexes")]
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

        _print_asof(asof_entries, args.stale_policy)

    elif args.command == "batch":
        client = api.ScopusClient(context=http_ctx)
        year_range = _parse_year_range(args.years, parser, announce=args.years is None)
        utils.process_batch_summary(args.input, args.output, client, year_range=year_range)

    elif args.command == "find":
        client = api.ScopusClient(context=http_ctx)
        try:
            fetched = client.find_papers(
                title=args.title, doi=args.doi, author_last_name=args.find_last_name,
                limit=args.limit, include_abstract=args.include_abstract)
        except ValueError as e:
            parser.error(f"find: {e}")
        if not fetched.complete:
            print(f"WARNING: {fetched.reason}", file=sys.stderr)
        if args.format == "json":
            _emit_json({
                "query": {"title": args.title, "doi": args.doi,
                          "author_last_name": args.find_last_name},
                "total_count": fetched.expected_total,
                "returned_count": len(fetched.papers),
                "papers": fetched.papers,
            }, args.output)
        else:
            _emit_text(lambda: utils.print_found_papers(fetched.papers,
                                                        fetched.expected_total),
                       args.output)

    elif args.command == "kaken-search":
        if not args.researcher_id and not args.name:
            parser.error("kaken-search requires --name or --id")
        client = kaken.KakenClient(context=http_ctx)
        if args.researcher_id:
            r = client.search_researcher_by_id(args.researcher_id, lang=args.lang)
            utils.print_kaken_researcher_results(args.researcher_id, [r] if r else [])
        else:
            results = client.search_researcher_by_name(args.name, lang=args.lang)
            utils.print_kaken_researcher_results(args.name, results)

    elif args.command == "mcp":
        from scopus_tools import mcp_server

        # stdio は MCP プロトコル専用。ログが混ざらないよう stderr に固定する。
        utils.setup_logging(stream=sys.stderr)
        # MCP SDK の import は build_server() まで遅延するので、ここで捕まえる。
        try:
            mcp_server.run(
                projects_dir=args.projects_dir,
                scie_list=args.scie_list,
                scie_dir=args.scie_dir,
                cache_db=args.cache_db,
                no_cache=args.no_cache,
                stale_policy=args.stale_policy,
                timeout=args.timeout,
            )
        except ImportError as e:
            parser.error(
                "mcp: the MCP SDK is not installed. "
                "Install with: pip install -e \".[mcp]\"  "
                f"(import error: {e})"
            )
        return

    elif args.command == "kaken-summary":
        client = kaken.KakenClient(context=http_ctx)
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
