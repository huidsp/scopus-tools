def compute_indices(citations):
    """H-indexおよびG-indexを計算する"""
    if not citations:
        return 0, 0
    
    s = sorted(citations, reverse=True)

    # H-index
    h = 0
    for i, c in enumerate(s, start=1):
        if c >= i:
            h = i
        else:
            break

    # G-index
    g = 0
    total = 0
    for i, c in enumerate(s, start=1):
        total += c
        if total >= i * i:
            g = i
            
    return h, g

def resolve_year_range(year_range=None, recent_years=5, current_year=None):
    """集計対象の年範囲を正規化する。未指定時は直近 recent_years 年を返す。"""
    import datetime

    current_year = current_year or datetime.datetime.now().year
    if year_range is not None:
        return year_range

    recent_years = max(1, int(recent_years))
    start_y = current_year - (recent_years - 1)
    return start_y, current_year


def default_eval_year_range(default_years=5, current_year=None):
    """ユーザ向けの「評価期間」既定値: **前年を含む** 直近 default_years 年。

    例: 2026 年中の実行 → (2021, 2025)
    集計のフィルタは年単位で行うため、進行中の今年を含めると半年ぶんしか
    データが揃わず歪むのを避けるための既定。
    """
    import datetime

    current_year = current_year or datetime.datetime.now().year
    end_y = current_year - 1
    start_y = end_y - (default_years - 1)
    return (start_y, end_y)

YEAR_RANGE_HELP = (
    "Accepted forms: 2021-2025, 2021,2025, 2021:2025, [2021,2025]"
)


def parse_year_range(text, default_years=5):
    """年範囲文字列をパースして (start, end) を返す純関数。

    受理する書式: '[2021,2025]', '2021,2025', '2021-2025', '2021:2025'。
    text が None なら `default_eval_year_range(default_years)`。
    不正な書式や start > end なら ValueError を送出する
    (呼び出し側が argparse の parser.error / MCP のエラー応答に変換する)。
    """
    if text is None:
        return default_eval_year_range(default_years=default_years)

    raw = str(text).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    for sep in (",", "-", ":"):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep)]
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                start_y, end_y = int(parts[0]), int(parts[1])
                if start_y > end_y:
                    raise ValueError(
                        f"start year must be <= end year (got {start_y} > {end_y})"
                    )
                return (start_y, end_y)
            break
    raise ValueError(f"year range '{text}' is invalid. {YEAR_RANGE_HELP}")


def summarize_papers(papers, recent_years=5, year_range=None):
    """論文リストから統計情報を抽出する"""
    import datetime
    current_year = datetime.datetime.now().year

    if not papers:
        return {
            "has_data": False,
            "total_count": 0,
            "total_citations": 0,
            "h_index": 0,
            "g_index": 0,
            "recent_count": 0,
            "recent_citations": 0,
            "total_first_author": 0,
            "recent_first_author": 0,
            "research_years": 0,
            "start_year": None,
            "has_scie_data": False,
            "total_scie": 0,
            "total_scie_first_author": 0,
            "recent_scie": 0,
            "recent_scie_first_author": 0,
        }

    cites = [p["citations"] for p in papers]
    h, g = compute_indices(cites)

    start_y, end_y = resolve_year_range(year_range=year_range, recent_years=recent_years, current_year=current_year)
    recent_papers = [p for p in papers if start_y <= p["year"] <= end_y]

    # 発行年が取れなかった論文は year=0 になる。これを最小値に含めると
    # research_years が current_year+1 になり、報告書に「研究年数 2027 年」が出る。
    known_years = [p["year"] for p in papers if p.get("year")]
    start_year = min(known_years) if known_years else None
    research_years = current_year - start_year + 1 if start_year else 0

    total_first = sum(1 for p in papers if p.get("is_first_author"))
    recent_first = sum(1 for p in recent_papers if p.get("is_first_author"))

    # SCIE 集計(scie.annotate_papers* で is_scie が付与されている場合のみ意味を持つ)。
    has_scie_data = any("is_scie" in p for p in papers)
    total_scie = sum(1 for p in papers if p.get("is_scie"))
    total_scie_first = sum(1 for p in papers if p.get("is_scie") and p.get("is_first_author"))
    recent_scie = sum(1 for p in recent_papers if p.get("is_scie"))
    recent_scie_first = sum(1 for p in recent_papers if p.get("is_scie") and p.get("is_first_author"))

    return {
        "has_data": True,
        "total_count": len(papers),
        "total_citations": sum(cites),
        "h_index": h,
        "g_index": g,
        "recent_count": len(recent_papers),
        "recent_citations": sum(p["citations"] for p in recent_papers),
        "total_first_author": total_first,
        "recent_first_author": recent_first,
        "research_years": research_years,
        "start_year": start_year,
        "has_scie_data": has_scie_data,
        "total_scie": total_scie,
        "total_scie_first_author": total_scie_first,
        "recent_scie": recent_scie,
        "recent_scie_first_author": recent_scie_first,
    }