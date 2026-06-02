import logging
import sys
import pandas as pd
from scopus_tools.core import resolve_year_range

# レポート表示の共通定数 / ヘルパー。
# ラベル幅は各関数のローカル事情(日本語幅含む)があるため引数で指定する設計。
SEP_WIDTH = 60


def _hr(char="="):
    """SEP_WIDTH 幅の区切り線文字列を返す。"""
    return char * SEP_WIDTH


def _section(label):
    """『【XXX】』形式のセクション見出しを改行付きで返す。"""
    return f"\n【{label}】"


def format_author_position(p):
    """著者順位を 'n/m' 形式で返す(筆頭著者なら ' (first)' を付す)。

    順位・著者数が不明なら空文字列。
    """
    pos = p.get("author_position")
    cnt = p.get("author_count")
    if not pos or not cnt:
        return ""
    s = f"{pos}/{cnt}"
    if pos == 1:
        s += " (first)"
    return s


def format_wos_indexes(p):
    """論文に付与された WoS 収録インデックスの表示文字列を返す。

    `wos_indexes`(リスト)があれば '[SCIE][SSCI]' 形式、空なら '[未収録]'。
    旧 `is_scie`(bool)のみの注釈には '[SCIE]'/'[非SCIE]' で後方互換。
    注釈が無ければ空文字列。
    """
    if "wos_indexes" in p:
        idx = p.get("wos_indexes") or []
        return "".join(f"[{x}]" for x in idx) if idx else "[未収録]"
    if "is_scie" in p:
        return "[SCIE]" if p.get("is_scie") else "[非SCIE]"
    return ""


def setup_logging(level=logging.INFO):
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

def read_input_csv(file_path, required_cols=None):
    """CSVを読み込む。required_cols を指定すると不足列があれば ValueError を上げる。"""
    df = pd.read_csv(file_path)
    if required_cols:
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(
                f"Input CSV '{file_path}' is missing required column(s): "
                f"{', '.join(missing)}. Found columns: {', '.join(df.columns)}"
            )
    return df


def progress(msg):
    """進捗 1 行を stderr に出力する(TTY なら同一行を上書き、非 TTY なら改行付き)。"""
    if sys.stderr.isatty():
        print(f"\r\033[K{msg}", end="", file=sys.stderr, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)


def progress_done():
    """TTY 上の進捗行を改行で確定させる。"""
    if sys.stderr.isatty():
        print("", file=sys.stderr, flush=True)

def save_output_csv(data_list, file_path):
    """リスト形式のデータをCSVとして保存する"""
    df = pd.DataFrame(data_list)
    df.to_csv(file_path, index=False, encoding='utf-8-sig')

def print_author_results(name, results):
    """著者検索結果をコンソールに表示する"""
    if not results:
        print(f"No results found for: {name}")
        return
    print(f"\n{_hr()}")
    print(f"Results for '{name}'")
    print(_hr())
    print(f"\nFound {len(results)} unique author(s):\n")
    for r in results:
        print(f"  Scopus ID  : {r['id']}")
        print(f"  Name       : {r['name']}")
        print(f"  Affiliation: {r['affiliation']}")
        print(f"  Documents  : {r['doc_count']}")
        print(_hr("-"))

def process_author_csv(input_path, output_path, client):
    """CSV の Name 列を一括検索して所属機関別に Scopus ID をまとめたCSVを出力する"""
    df = read_input_csv(input_path, required_cols=["Name"])
    total = len(df)
    rows = []
    for idx, row in enumerate(df.itertuples(index=False), start=1):
        name = str(getattr(row, "Name"))
        progress(f"[{idx}/{total}] searching: {name}")
        found = client.search_author_by_name(name)
        # 所属機関別にIDをグループ化（旧 get_author.py の process_csv と同等）
        affiliation_dict = {}
        for author in found:
            aff = author["affiliation"] or "No information"
            affiliation_dict.setdefault(aff, []).append(author["id"])
        if affiliation_dict:
            for aff, ids in affiliation_dict.items():
                rows.append({"Name": name, "Scopus ID": ",".join(ids), "Affiliation": aff})
        else:
            rows.append({"Name": name, "Scopus ID": "", "Affiliation": ""})
    progress_done()
    save_output_csv(rows, output_path)

def print_report_text(first, last, s_ids, report, papers, recent_years=5, year_range=None):
    """人間が読みやすい形式でサマリーを表示する（旧 print_summary と同等）"""
    import datetime
    current_year = datetime.datetime.now().year
    print(_hr())
    print(f"Scopus IDs: {', '.join(s_ids)}")
    print(f"Name      : {first} {last}".strip())
    print(_hr())
    print(f"研究歴: {report['start_year']}年〜{current_year}年（{report['research_years']}年間）")
    print(_section("全期間"))
    print(f"  総論文数          : {report['total_count']}")
    print(f"  総引用回数        : {report['total_citations']}")
    print(f"  筆頭著者論文数    : {report['total_first_author']}")
    if report.get("has_scie_data"):
        print(f"  SCI(SCIE)論文数   : {report['total_scie']}（うち筆頭著者 {report['total_scie_first_author']}）")
    recent_start, recent_end = resolve_year_range(
        year_range=year_range,
        recent_years=recent_years,
        current_year=current_year,
    )
    print(f"{_section('指定した年の集計')}（{recent_start}年〜{recent_end}年）")
    print(f"  論文数            : {report['recent_count']}")
    print(f"  総引用回数        : {report['recent_citations']}")
    print(f"  筆頭著者論文数    : {report['recent_first_author']}")
    if report.get("has_scie_data"):
        print(f"  SCI(SCIE)論文数   : {report['recent_scie']}（うち筆頭著者 {report['recent_scie_first_author']}）")
    print(_section("引用指標"))
    print(f"  H-index: {report['h_index']}")
    print(f"  G-index: {report['g_index']}")
    top5 = sorted(papers, key=lambda x: x["citations"], reverse=True)[:5]
    print(_section("被引用数上位5件"))
    for i, p in enumerate(top5, 1):
        print(f"  {i}. {p['title']}")
        if p.get("authors"):
            print(f"     著者      : {p['authors']}")
        pos = format_author_position(p)
        if pos:
            print(f"     著者順位  : {pos}")
        if p.get("journal"):
            agg = f" [{p['aggregation_type']}]" if p.get("aggregation_type") else ""
            wos = format_wos_indexes(p)
            wos = f" {wos}" if wos else ""
            print(f"     ジャーナル: {p['journal']}{agg}{wos}")
        biblio = []
        if p.get("volume"):  biblio.append(f"Vol.{p['volume']}")
        if p.get("issue"):   biblio.append(f"No.{p['issue']}")
        if p.get("pages"):   biblio.append(f"pp.{p['pages']}")
        if p.get("year"):    biblio.append(str(p["year"]))
        if biblio:
            print(f"     書誌      : {', '.join(biblio)}")
        fa = "  [筆頭著者]" if p.get("is_first_author") else ""
        print(f"     引用数    : {p['citations']}{fa}")
        if p.get("eid"):
            print(f"     EID       : {p['eid']}")
        print("")

def print_papers_list(first, last, s_ids, papers, year_range, header=True):
    """指定年範囲の論文一覧を人間が読みやすい形式で表示する。

    papers は呼び出し側で年範囲フィルタ済みを想定。新しい順に並べて表示する。
    header=False ならタイトル等のヘッダ行を省略し一覧本体のみ出力する(WebUI 再利用用)。
    """
    start_y, end_y = year_range
    if header:
        print(_hr())
        print(f"Scopus IDs: {', '.join(s_ids)}")
        print(f"Name      : {first} {last}".strip())
        print(f"対象期間  : {start_y}年〜{end_y}年")
        print(f"該当論文数: {len(papers)} 件")
        print(_hr())
    ordered = sorted(papers, key=lambda x: (x.get("year", 0), x.get("citations", 0)), reverse=True)
    for i, p in enumerate(ordered, 1):
        print(f"\n  {i}. {p.get('title') or '(無題)'}")
        if p.get("authors"):
            print(f"     著者      : {p['authors']}")
        pos = format_author_position(p)
        if pos:
            print(f"     著者順位  : {pos}")
        if p.get("journal"):
            agg = f" [{p['aggregation_type']}]" if p.get("aggregation_type") else ""
            wos = format_wos_indexes(p)
            wos = f" {wos}" if wos else ""
            print(f"     ジャーナル: {p['journal']}{agg}{wos}")
        biblio = []
        if p.get("volume"):  biblio.append(f"Vol.{p['volume']}")
        if p.get("issue"):   biblio.append(f"No.{p['issue']}")
        if p.get("pages"):   biblio.append(f"pp.{p['pages']}")
        if p.get("year"):    biblio.append(str(p["year"]))
        if biblio:
            print(f"     書誌      : {', '.join(biblio)}")
        if p.get("type"):
            print(f"     種別      : {p['type']}")
        print(f"     引用数    : {p.get('citations', 0)}")
        if p.get("eid"):
            print(f"     EID       : {p['eid']}")


def save_papers_csv(rows, output_path):
    """論文一覧を CSV に保存する(Excel 互換の utf-8-sig)。

    rows は (s_ids, first, last, papers) のリスト。複数著者ぶんを 1 ファイルに縦結合し、
    どの著者の論文かを Name / Scopus IDs 列で識別できるようにする。
    """
    # WoS インデックス注釈(--scie-list 指定時のみ各論文に wos_indexes が付く)があれば列を出す。
    has_wos = any("wos_indexes" in p for _, _, _, papers in rows for p in papers)
    fieldnames = [
        "Name", "Scopus IDs", "Year", "Title", "Authors", "Journal",
        "ISSN", "eISSN", "Volume", "Issue", "Pages", "Type", "Citations",
        "Author Position", "Author Count", "First Author", "EID",
    ]
    if has_wos:
        fieldnames.append("WoS Index")
    out = []
    for s_ids, first, last, papers in rows:
        name = f"{first} {last}".strip()
        ids = ", ".join(s_ids)
        ordered = sorted(papers, key=lambda x: (x.get("year", 0), x.get("citations", 0)), reverse=True)
        for p in ordered:
            row = {
                "Name": name,
                "Scopus IDs": ids,
                "Year": p.get("year", ""),
                "Title": p.get("title", ""),
                "Authors": p.get("authors", ""),
                "Journal": p.get("journal", ""),
                "ISSN": p.get("issn", ""),
                "eISSN": p.get("eissn", ""),
                "Volume": p.get("volume", ""),
                "Issue": p.get("issue", ""),
                "Pages": p.get("pages", ""),
                "Type": p.get("type", ""),
                "Citations": p.get("citations", 0),
                "Author Position": format_author_position(p),
                "Author Count": p.get("author_count", ""),
                "First Author": "Yes" if p.get("is_first_author") else "",
                "EID": p.get("eid", ""),
            }
            if has_wos:
                row["WoS Index"] = "|".join(p.get("wos_indexes") or [])
            out.append(row)
    df = pd.DataFrame(out, columns=fieldnames)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")


def print_kaken_researcher_results(query, results):
    """KAKEN研究者検索結果をコンソールに表示する"""
    if not results:
        print(f"No KAKEN researcher found for: {query}")
        return
    print(f"\n{_hr()}")
    print(f"KAKEN Researcher results for '{query}'")
    print(f"{_hr()}\n")
    print(f"Found {len(results)} researcher(s):\n")
    for r in results:
        print(f"  研究者番号  : {r.get('researcher_id', '')}")
        print(f"  氏名        : {r.get('name', '')}")
        if r.get("name_kana"):
            print(f"  カナ        : {r['name_kana']}")
        print(f"  所属        : {r.get('affiliation', '')}")
        if r.get("department"):
            print(f"  部局        : {r['department']}")
        if r.get("job_title"):
            print(f"  職名        : {r['job_title']}")
        if r.get("project_count"):
            print(f"  研究課題数  : {r['project_count']}")
        print(_hr("-"))


def print_kaken_summary(researcher_id, researcher, grants):
    """KAKEN獲得課題サマリーを人間可読形式で表示する"""
    print(_hr())
    print(f"研究者番号 : {researcher_id}")
    if researcher:
        print(f"氏名       : {researcher.get('name', '')}")
        if researcher.get("affiliation"):
            print(f"所属       : {researcher['affiliation']}")
    print(_hr())

    if not grants:
        print("該当する科研費課題は見つかりませんでした。")
        return

    # 統計
    role_counts = {}
    type_counts = {}
    for g in grants:
        role = g.get("my_role") or "(未確定)"
        role_counts[role] = role_counts.get(role, 0) + 1
        gt = g.get("grant_category") or "(不明)"
        type_counts[gt] = type_counts.get(gt, 0) + 1

    print(f"{_section('獲得課題総数')} {len(grants)} 件")
    print(_section("役割別"))
    for role, n in sorted(role_counts.items(), key=lambda x: -x[1]):
        print(f"  {role:30s}: {n} 件")
    print(_section("研究種目別"))
    for gt, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {gt:30s}: {n} 件")

    print(_section("総配分額"))
    total_sum = sum(int(g["total_cost"]) for g in grants if str(g.get("total_cost") or "").isdigit())
    direct_sum = sum(int(g["direct_cost"]) for g in grants if str(g.get("direct_cost") or "").isdigit())
    print(f"  直接経費合計: {direct_sum:,} 円")
    print(f"  総額合計    : {total_sum:,} 円")

    print(_section("課題一覧（古い順）"))
    for i, g in enumerate(grants, 1):
        period = ""
        if g.get("period_from") and g.get("period_to"):
            period = f"{g['period_from']}〜{g['period_to']}年度"
        elif g.get("period_from"):
            period = f"{g['period_from']}年度〜"
        print(f"\n  {i}. {g.get('title') or '(無題)'}")
        if g.get("project_number"):
            print(f"     課題番号 : {g['project_number']}")
        if g.get("grant_category"):
            cat = g["grant_category"]
            if g.get("allocation"):
                cat = f"{cat} ({g['allocation']})"
            print(f"     研究種目 : {cat}")
        if g.get("review_section"):
            print(f"     審査区分 : {g['review_section']}")
        if period:
            print(f"     研究期間 : {period}")
        if g.get("institution"):
            print(f"     研究機関 : {g['institution']}")
        if g.get("status"):
            print(f"     ステータス: {g['status']}")
        if g.get("my_role"):
            print(f"     役割     : {g['my_role']}")
        if g.get("total_cost") and str(g["total_cost"]).isdigit():
            tc = int(g["total_cost"])
            dc = int(g["direct_cost"]) if str(g.get("direct_cost") or "").isdigit() else None
            line = f"     配分額   : 総額 {tc:,} 円"
            if dc is not None:
                line += f"（直接経費 {dc:,} 円）"
            print(line)
        if g.get("keywords"):
            kws = ", ".join(g["keywords"][:8])
            print(f"     キーワード: {kws}")


def process_batch_summary(input_path, output_path, client, year_range=None):
    """CSV の Scopus ID 列を一括処理してサマリーCSVを出力する"""
    import csv
    from scopus_tools.core import summarize_papers
    df = read_input_csv(input_path, required_cols=["Scopus ID"])
    total = len(df)
    results = []
    for idx, (_, row) in enumerate(df.iterrows(), start=1):
        name = row.get("Name", "")
        scopus_id_value = row.get("Scopus ID")
        affiliation = row.get("Affiliation", "")
        if not scopus_id_value or (hasattr(scopus_id_value, "__class__") and str(scopus_id_value) == "nan"):
            logging.warning("Missing Scopus ID for %s, skipping.", name)
            continue
        progress(f"[{idx}/{total}] processing: {name or scopus_id_value}")
        s_ids = [s.strip() for s in str(scopus_id_value).split(",") if s.strip()]
        first, last = client.get_author_profile(s_ids[0])
        papers = client.search_papers(s_ids)
        report = summarize_papers(papers, year_range=year_range)
        if not report["has_data"]:
            logging.warning("No data found for %s", name)
            continue
        results.append({
            "Name": name,
            "Scopus IDs": ", ".join(s_ids),
            "Affiliation": affiliation,
            "Research Years": report["research_years"],
            "Start Year": report["start_year"],
            "Total Papers": report["total_count"],
            "Total Citations": report["total_citations"],
            "Total First Author": report["total_first_author"],
            "Recent 5Y Papers": report["recent_count"],
            "Recent 5Y Citations": report["recent_citations"],
            "Recent 5Y First Author": report["recent_first_author"],
            "H-index": report["h_index"],
            "G-index": report["g_index"],
        })
    progress_done()
    if results:
        fieldnames = [
            "Name", "Scopus IDs", "Affiliation", "Research Years", "Start Year",
            "Total Papers", "Total Citations", "Total First Author",
            "Recent 5Y Papers", "Recent 5Y Citations", "Recent 5Y First Author",
            "H-index", "G-index",
        ]
        with open(output_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
    else:
        logging.warning("No results to write.")