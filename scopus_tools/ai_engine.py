import os
from openai import OpenAI

def estimate_expertise(papers, lang="ja"):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "OpenAI API key not found. Skipping analysis."
    
    client = OpenAI(api_key=api_key)
    # 上位論文と最新論文のタイトルをコンテキストにする
    top_papers = sorted(papers, key=lambda x: x['citations'], reverse=True)[:10]
    titles = [p['title'] for p in top_papers]
    
    prompt = f"""
    The following is a list of research paper titles by a specific researcher:
    {chr(10).join(titles)}

    Based on these titles, please provide:
    1. A concise summary of their primary research field (Expertise).
    2. 3-5 key technical terms that define their work.
    
    Respond in {lang}.
    """

    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


def _build_journal_summary(papers):
    """論文リストからジャーナル別の掲載件数と総被引用数をまとめた辞書リストを返す。"""
    from collections import defaultdict
    journal_stats = defaultdict(lambda: {"count": 0, "citations": 0, "type": ""})
    for p in papers:
        journal = (p.get("journal") or "").strip()
        if not journal:
            continue
        journal_stats[journal]["count"] += 1
        journal_stats[journal]["citations"] += p.get("citations", 0)
        if not journal_stats[journal]["type"]:
            journal_stats[journal]["type"] = p.get("aggregation_type", "")
    return sorted(
        [{"journal": j, **v} for j, v in journal_stats.items()],
        key=lambda x: x["citations"],
        reverse=True,
    )


def _infer_field_context(client, all_titles):
    """論文タイトル群から研究分野と分野固有の業績基準をJSON形式で推定する。"""
    sample = "\n".join(f"  - {t}" for t in all_titles[:20])
    prompt = f"""以下の論文タイトルから研究者の主要な研究分野を推定し、
その分野における一般的な業績基準をJSON形式で返してください。

【論文タイトル（一部）】
{sample}

返すJSONのキーと内容:
{{
  "field": "分野名（例: 計算機科学, 生命科学, 材料工学 など）",
  "sub_fields": ["サブ分野1", "サブ分野2"],
  "citation_norm": "その分野での論文1本あたりの典型的な被引用数の説明（例: 低め 5〜20件程度）",
  "hindex_norm": "その分野でのキャリア別H-indexの目安（例: 中堅研究者で10〜20程度）",
  "pub_rate_norm": "その分野での年間発表論文数の目安（例: 年3〜8本程度）",
  "notes": "評価上の注意点（例: 国際会議論文が主流で雑誌論文が少ない傾向がある、など）"
}}

JSONのみを返してください。"""

    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    import json
    try:
        return json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return {}


def evaluate_achievements(papers, report, lang="ja"):
    """業績指標と論文リストに基づいてAIが研究者を総合評価する。
    論文タイトルから研究分野を推定し、その分野の慣例に照らした相対評価を行う。
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "OpenAI API key not found. Skipping evaluation."

    client = OpenAI(api_key=api_key)

    all_titles = [p["title"] for p in papers]
    field_ctx = _infer_field_context(client, all_titles)

    top_papers = sorted(papers, key=lambda x: x["citations"], reverse=True)[:10]
    top_titles = "\n".join(
        f"  - {p['title']} [{p.get('journal') or '不明'}] (citations: {p['citations']}, year: {p['year']})"
        for p in top_papers
    )

    journal_summary = _build_journal_summary(papers)
    journal_lines = "\n".join(
        f"  - {j['journal']} ({j['type'] or '種別不明'}): {j['count']} 件, 総被引用 {j['citations']} 件"
        for j in journal_summary[:15]
    )

    field_section = ""
    if field_ctx:
        sub = ", ".join(field_ctx.get("sub_fields", []))
        field_section = f"""
【推定研究分野】
- 分野: {field_ctx.get('field', '不明')}
- サブ分野: {sub or '不明'}
- 分野における被引用数の目安: {field_ctx.get('citation_norm', '不明')}
- 分野におけるH-indexの目安: {field_ctx.get('hindex_norm', '不明')}
- 分野における年間発表数の目安: {field_ctx.get('pub_rate_norm', '不明')}
- 評価上の注意点: {field_ctx.get('notes', 'なし')}
"""

    prompt = f"""以下は、ある研究者の業績データです。
{field_section}
【統計指標】
- 総論文数: {report['total_count']} 件
- 総被引用数: {report['total_citations']} 件
- H-index: {report['h_index']}
- G-index: {report['g_index']}
- 研究開始年: {report.get('start_year', '不明')}（研究歴 {report['research_years']} 年）
- 評価期間内の論文数: {report['recent_count']} 件
- 評価期間内の被引用数: {report['recent_citations']} 件
- 評価期間内の筆頭著者論文数: {report['recent_first_author']} 件

【掲載ジャーナル一覧（被引用数順、上位15誌）】
{journal_lines}

【被引用数上位論文（ジャーナル名含む）】
{top_titles}

上記データをもとに、推定された研究分野の慣例・水準と照らし合わせながら、以下の観点で忖度なく研究者を総合評価してください：
1. 研究の生産性（論文数・継続性）― 分野平均と比較して
2. 研究の影響力（被引用数・H-index・G-index）― 分野水準と比較して
3. 掲載ジャーナルの質（分野内での評価・Impact Factor水準・Q1/Q2/Q3/Q4の傾向）
4. 評価期間における最近の活動度
5. 研究分野・専門性の特徴と独自性
6. 総合評価コメント（強み・改善点・今後の期待）

分野の違いによるバイアスを補正した上で、具体的かつ建設的に{lang}で記述してください。
"""

    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content