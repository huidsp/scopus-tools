import logging
import pandas as pd
from scopus_tools.core import resolve_year_range

def setup_logging(level=logging.INFO):
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

def read_input_csv(file_path):
    """Name, Scopus ID, Affiliation を含むCSVを読み込む"""
    df = pd.read_csv(file_path)
    return df

def save_output_csv(data_list, file_path):
    """リスト形式のデータをCSVとして保存する"""
    df = pd.DataFrame(data_list)
    df.to_csv(file_path, index=False, encoding='utf-8-sig')

def print_author_results(name, results):
    """著者検索結果をコンソールに表示する"""
    if not results:
        print(f"No results found for: {name}")
        return
    print(f"\n{'='*60}")
    print(f"Results for '{name}'")
    print(f"{'='*60}")
    print(f"\nFound {len(results)} unique author(s):\n")
    for r in results:
        print(f"  Scopus ID  : {r['id']}")
        print(f"  Name       : {r['name']}")
        print(f"  Affiliation: {r['affiliation']}")
        print(f"  Documents  : {r['doc_count']}")
        print("-" * 60)

def process_author_csv(input_path, output_path, client):
    """CSV の Name 列を一括検索して所属機関別に Scopus ID をまとめたCSVを出力する"""
    df = read_input_csv(input_path)
    rows = []
    for _, row in df.iterrows():
        name = str(row["Name"])
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
    save_output_csv(rows, output_path)

def print_report_text(first, last, s_ids, report, papers, recent_years=5, year_range=None):
    """人間が読みやすい形式でサマリーを表示する（旧 print_summary と同等）"""
    import datetime
    current_year = datetime.datetime.now().year
    print("=" * 60)
    print(f"Scopus IDs: {', '.join(s_ids)}")
    print(f"Name      : {first} {last}".strip())
    print("=" * 60)
    print(f"研究歴: {report['start_year']}年〜{current_year}年（{report['research_years']}年間）")
    print("\n【全期間】")
    print(f"  総論文数          : {report['total_count']}")
    print(f"  総引用回数        : {report['total_citations']}")
    print(f"  筆頭著者論文数    : {report['total_first_author']}")
    recent_start, recent_end = resolve_year_range(
        year_range=year_range,
        recent_years=recent_years,
        current_year=current_year,
    )
    print(f"\n【指定した年の集計】（{recent_start}年〜{recent_end}年）")
    print(f"  論文数            : {report['recent_count']}")
    print(f"  総引用回数        : {report['recent_citations']}")
    print(f"  筆頭著者論文数    : {report['recent_first_author']}")
    print("\n【引用指標】")
    print(f"  H-index: {report['h_index']}")
    print(f"  G-index: {report['g_index']}")
    top5 = sorted(papers, key=lambda x: x["citations"], reverse=True)[:5]
    print("\n【被引用数上位5件】")
    for i, p in enumerate(top5, 1):
        print(f"  {i}. {p['title']}")
        if p.get("authors"):
            print(f"     著者      : {p['authors']}")
        if p.get("journal"):
            agg = f" [{p['aggregation_type']}]" if p.get("aggregation_type") else ""
            print(f"     ジャーナル: {p['journal']}{agg}")
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

def process_batch_summary(input_path, output_path, client, year_range=None):
    """CSV の Scopus ID 列を一括処理してサマリーCSVを出力する"""
    import csv
    from scopus_tools.core import summarize_papers
    df = read_input_csv(input_path)
    results = []
    for _, row in df.iterrows():
        name = row.get("Name", "")
        scopus_id_value = row.get("Scopus ID")
        affiliation = row.get("Affiliation", "")
        if not scopus_id_value or (hasattr(scopus_id_value, "__class__") and str(scopus_id_value) == "nan"):
            logging.warning("Missing Scopus ID for %s, skipping.", name)
            continue
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