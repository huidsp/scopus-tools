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

def summarize_papers(papers, recent_years=5):
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

    recent_start = current_year - (recent_years - 1)
    recent_papers = [p for p in papers if p["year"] >= recent_start]

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