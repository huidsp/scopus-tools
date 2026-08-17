import csv
import logging
import sys
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


# DEBUG にすると完全な URL をログに出すライブラリ。Scopus は apiKey を
# クエリパラメータで渡すため、これを抑えないと API キーがそのままログに残る。
# MCP サーバの stderr は MCP クライアントがログファイルに保存する(場合によっては
# 誰でも読める権限で)ので、実害がある。
_URL_LOGGING_LIBRARIES = ("urllib3", "requests", "httpx", "httpcore")


def setup_logging(level=logging.INFO, stream=None):
    """ロギングを初期化する。

    stream を渡すと既存ハンドラを置き換えてそのストリームに固定する
    (MCP サーバのように stdout を汚せない場合に stderr を明示するため)。

    アプリ側を DEBUG にしても API キーが漏れないよう、URL を丸ごと出力する
    ライブラリのロガーは WARNING に固定する。
    """
    kwargs = {
        "level": level,
        "format": '%(asctime)s - %(levelname)s - %(message)s',
    }
    if stream is not None:
        kwargs["stream"] = stream
        kwargs["force"] = True
    logging.basicConfig(**kwargs)
    silence_url_logging()


def silence_url_logging():
    """URL を丸ごとログに出すライブラリを WARNING に落とす(API キー漏洩の防止)。

    `setup_logging` を経由しない呼び出し側(ライブラリとして import した場合など)
    からも直接呼べるようにしておく。
    """
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

class CsvRows(list):
    """CSV を読んだ結果。dict の list に列名 `columns` を添えたもの。

    以前は pandas の DataFrame を返していた。呼び出し側が使っていたのは
    `.columns` と行の反復だけなので、標準ライブラリの csv で置き換えている
    (pandas + numpy で 104MB あり、用途に対して重すぎた)。
    """

    def __init__(self, rows=(), columns=()):
        super().__init__(rows)
        self.columns = list(columns)


def read_input_csv(file_path, required_cols=None):
    """CSVを読み込む。required_cols を指定すると不足列があれば ValueError を上げる。

    戻り値は `CsvRows`(dict の list + `.columns`)。空セルは空文字になる
    (pandas 時代の NaN ではない)。
    """
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = [c for c in (reader.fieldnames or []) if c is not None]
        rows = [
            {k: ("" if v is None else v) for k, v in row.items() if k is not None}
            for row in reader
        ]
    if required_cols:
        missing = [c for c in required_cols if c not in columns]
        if missing:
            raise ValueError(
                f"Input CSV '{file_path}' is missing required column(s): "
                f"{', '.join(missing)}. Found columns: {', '.join(columns)}"
            )
    return CsvRows(rows, columns)


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
    """リスト形式のデータをCSVとして保存する。

    列は **全行のキーの和集合**(出現順)。pandas の `DataFrame(list).to_csv()` が
    そうしていたので、行ごとにキーが違っても列が落ちない挙動を保つ。
    """
    fieldnames = []
    for row in data_list or []:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    write_csv(data_list, file_path, fieldnames)


def write_csv(rows, file_path, fieldnames):
    """dict の list を CSV に書き出す。

    - `utf-8-sig`: Excel で日本語が化けないようにするため(従来どおり)。
    - `lineterminator="\\n"`: csv モジュールの既定は CRLF だが、pandas 時代の出力は
      LF だった。既存の出力と 1 バイトも変えないために LF を明示する。
    """
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        for row in rows or []:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

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

def process_author_csv(input_path, output_path, client, try_both=False):
    """CSV を一括検索して所属機関別に Scopus ID をまとめた CSV を出力する。

    `First Name` / `Last Name` 列があればそれを使い(1 リクエスト、姓名の取り違えなし)、
    無ければ `Name` 列を空白で分割して **given surname の順を仮定**する。
    仮定した場合は 1 度だけ警告を出す。
    """
    df = read_input_csv(input_path)
    has_split = "First Name" in df.columns and "Last Name" in df.columns
    if not has_split:
        missing = [c for c in ["Name"] if c not in df.columns]
        if missing:
            raise ValueError(
                f"Input CSV '{input_path}' needs either a 'Name' column or both "
                f"'First Name' and 'Last Name' columns. Found: {', '.join(df.columns)}"
            )
        print("Note: assuming the 'Name' column is 'given-name surname'. "
              "Add 'First Name'/'Last Name' columns to be explicit.", file=sys.stderr)

    total = len(df)
    rows = []
    for idx, row in enumerate(df, start=1):
        if has_split:
            first = str(row["First Name"] or "").strip()
            last = str(row["Last Name"] or "").strip()
            name = f"{first} {last}".strip()
            progress(f"[{idx}/{total}] searching: {name}")
            found = client.search_author(first, last)
        else:
            name = str(row["Name"])
            progress(f"[{idx}/{total}] searching: {name}")
            found = client.search_author_by_name(name, try_both=try_both)
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
        metrics = p.get("metrics")
        if metrics and metrics.get("citescore") is not None:
            # **年を必ず併記する。** どの年の指標か分からない数字は使えない。
            year = metrics.get("metric_year")
            mark = "" if metrics.get("year_match") == "exact" else "≈"
            prov = " 暫定" if metrics.get("provisional") else ""
            pct = metrics.get("percentile")
            rank = f" {pct}%ile {metrics.get('quartile')}" if pct is not None else ""
            print(f"     CiteScore : {metrics['citescore']} ({mark}{year}年{prov}){rank}")
        jcr_rec = p.get("jcr")
        if jcr_rec and jcr_rec.get("jif") is not None:
            print(f"     JIF       : {jcr_rec['jif']} "
                  f"({jcr_rec.get('quartile')}, JCR{jcr_rec.get('jcr_year')})")
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
        # 新しい列は末尾に足す(既存の列順を変えると下流の処理が壊れるため)
        "DOI", "Article Number", "Open Access",
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
                "DOI": p.get("doi", ""),
                "Article Number": p.get("article_number", ""),
                "Open Access": "Yes" if p.get("open_access") else "",
            }
            if has_wos:
                row["WoS Index"] = "|".join(p.get("wos_indexes") or [])
            out.append(row)
    write_csv(out, output_path, fieldnames)


def print_found_papers(papers, total=None):
    """`find` の結果を表示する。**著者は Scopus Author ID 付き**で並べる。

    この機能の主目的が「分裂した Author ID の特定」なので、ID が読み取れる形にする。
    """
    if not papers:
        print("該当する論文がありません。語数を減らすか、DOI を指定してください。")
        return
    if total is not None and total > len(papers):
        print(f"{total} 件が該当し、うち {len(papers)} 件を表示します。\n")
    for i, p in enumerate(papers):
        if i > 0:
            print(_hr("-"))
        print(f"{p.get('title', '')}")
        bits = [b for b in (p.get("journal"), str(p.get("year") or ""),
                            f"Vol.{p['volume']}" if p.get("volume") else "",
                            f"No.{p['issue']}" if p.get("issue") else "",
                            f"pp.{p['pages']}" if p.get("pages") else "",
                            f"Art.{p['article_number']}" if p.get("article_number") else "") if b]
        print("  " + " / ".join(bits))
        meta = [f"{p.get('type', '')}", f"被引用 {p.get('citations', 0)}"]
        if p.get("doi"):
            meta.append(f"DOI {p['doi']}")
        if p.get("issn") or p.get("eissn"):
            meta.append(f"ISSN {p.get('issn') or '-'} / eISSN {p.get('eissn') or '-'}")
        if p.get("open_access"):
            meta.append("OA")
        print("  " + " | ".join(m for m in meta if m))
        print("  著者:")
        for a in p.get("authors_detail") or []:
            orcid = f"  ORCID {a['orcid']}" if a.get("orcid") else ""
            print(f"    {str(a.get('name') or ''):20} authid={a.get('authid')}{orcid}")
        affs = [af.get("name") for af in (p.get("affiliations") or []) if af.get("name")]
        if affs:
            print(f"  所属: {', '.join(affs)}")
        if p.get("keywords"):
            print(f"  キーワード: {', '.join(p['keywords'])}")
        if p.get("abstract"):
            print(f"  抄録: {p['abstract'][:300]}...")
        print(f"  EID : {p.get('eid', '')}")


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
    for idx, row in enumerate(df, start=1):
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