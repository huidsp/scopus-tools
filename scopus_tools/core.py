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
        }

    cites = [p["citations"] for p in papers]
    h, g = compute_indices(cites)

    start_y, end_y = resolve_year_range(year_range=year_range, recent_years=recent_years, current_year=current_year)
    recent_papers = [p for p in papers if start_y <= p["year"] <= end_y]

    start_year = min(p["year"] for p in papers)
    research_years = current_year - start_year + 1

    total_first = sum(1 for p in papers if p.get("is_first_author"))
    recent_first = sum(1 for p in recent_papers if p.get("is_first_author"))

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
    }