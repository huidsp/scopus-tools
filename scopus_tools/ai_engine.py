import logging
import os

from scopus_tools import llm
from scopus_tools.llm import DEFAULT_MODEL

logger = logging.getLogger(__name__)


def estimate_expertise(papers, lang="ja", model=DEFAULT_MODEL):
    """論文タイトル群から研究者の専門分野を推定する。

    model で OpenAI / Claude のいずれかを選択(既定: gpt-5.4)。
    必要な API キー(モデルに応じて OPENAI_API_KEY か ANTHROPIC_API_KEY)が無ければ
    旧来挙動と同様にスキップメッセージを返す。
    """
    key = llm.required_key_for(model)
    if not os.getenv(key):
        return f"{key} not found. Skipping analysis."

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
    return llm.complete(model, prompt)


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


def _infer_field_context(model, all_titles):
    """論文タイトル群から研究分野と分野固有の業績基準をJSON形式で推定する。

    model でプロバイダを選択。JSON モード(OpenAI: response_format / Anthropic: プロンプト指示)
    で結果を取得し、parse_json_response で耐性パースする。
    """
    if not all_titles:
        return {}
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

    text = llm.complete(model, prompt, json_mode=True)
    return llm.parse_json_response(text)


def _build_kaken_summary(grants):
    """KAKEN獲得課題リストから集計サマリーとプロンプト用テキストを返す。"""
    if not grants:
        return None
    from collections import Counter

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    total_count = len(grants)
    role_counts = Counter((g.get("my_role") or "未確定") for g in grants)
    category_counts = Counter((g.get("grant_category") or "不明") for g in grants)
    pi_count = role_counts.get("principal_investigator", 0)
    coi_count = sum(v for k, v in role_counts.items() if "co_investigator" in k)
    direct_sum = sum(_int(g.get("direct_cost")) for g in grants)
    total_sum = sum(_int(g.get("total_cost")) for g in grants)

    # 年度範囲（最初の開始〜最後の終了）
    years_from = [int(g["period_from"]) for g in grants if str(g.get("period_from") or "").isdigit()]
    years_to = [int(g["period_to"]) for g in grants if str(g.get("period_to") or "").isdigit()]
    first_year = min(years_from) if years_from else None
    last_year = max(years_to) if years_to else None

    # 課題詳細（古い順）
    sorted_grants = sorted(grants, key=lambda g: (g.get("period_from") or "0"))
    detail_lines = []
    for g in sorted_grants:
        period = ""
        if g.get("period_from") and g.get("period_to"):
            period = f"{g['period_from']}〜{g['period_to']}"
        cost = ""
        if g.get("total_cost") and str(g["total_cost"]).isdigit():
            cost = f"総額 {int(g['total_cost']):,}円"
        role = g.get("my_role") or "-"
        cat = g.get("grant_category") or "-"
        detail_lines.append(
            f"  - [{period}] {cat} / {role} / {cost} / {g.get('institution', '-')}: "
            f"{g.get('title') or '(無題)'}"
        )

    return {
        "total_count": total_count,
        "pi_count": pi_count,
        "coi_count": coi_count,
        "direct_sum": direct_sum,
        "total_sum": total_sum,
        "first_year": first_year,
        "last_year": last_year,
        "category_counts": dict(category_counts),
        "detail_lines": detail_lines,
    }


def infer_field(papers, model=DEFAULT_MODEL):
    """論文タイトル群から研究分野を推定して dict を返す(WebUI から呼べる公開エイリアス)。

    model に応じた API キーが無ければ空 dict を返す。
    """
    if not papers:
        return {}
    if not os.getenv(llm.required_key_for(model)):
        return {}
    titles = [p.get("title", "") for p in papers if p.get("title")]
    return _infer_field_context(model, titles) if titles else {}


def _build_eval_prompt(papers, report, lang, grants, field_ctx, extra_instructions=None):
    """評価プロンプトの本文を組み立てる(stream/非 stream で共通)。

    extra_instructions に文字列を渡すと「【評価の観点・追加指示】」セクションが末尾に
    追加される(ユーザが UI から入力した自由形式の指示)。
    """
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

    kaken_section = ""
    kaken_summary = _build_kaken_summary(grants) if grants else None
    if kaken_summary:
        cat_lines = "\n".join(
            f"  - {cat}: {n} 件" for cat, n in
            sorted(kaken_summary["category_counts"].items(), key=lambda x: -x[1])
        )
        details = "\n".join(kaken_summary["detail_lines"])
        period_str = ""
        if kaken_summary["first_year"] and kaken_summary["last_year"]:
            period_str = f"{kaken_summary['first_year']}〜{kaken_summary['last_year']}年度"
        kaken_section = f"""
【科研費獲得実績（KAKEN）】
- 総獲得課題数: {kaken_summary['total_count']} 件
- 代表者として: {kaken_summary['pi_count']} 件
- 分担者として: {kaken_summary['coi_count']} 件
- 直接経費合計: {kaken_summary['direct_sum']:,} 円
- 総額合計    : {kaken_summary['total_sum']:,} 円
- ファンディング期間: {period_str}
- 種目別内訳:
{cat_lines}

【獲得課題詳細（古い順）】
{details}
"""

    eval_points = """1. 研究の生産性（論文数・継続性）― 分野平均と比較して
2. 研究の影響力（被引用数・H-index・G-index）― 分野水準と比較して
3. 掲載ジャーナルの質（分野内での評価・Impact Factor水準・Q1/Q2/Q3/Q4の傾向）
4. 評価期間における最近の活動度
5. 研究分野・専門性の特徴と独自性
6. 総合評価コメント（強み・改善点・今後の期待）"""
    if kaken_section:
        eval_points = """1. 研究の生産性（論文数・継続性）― 分野平均と比較して
2. 研究の影響力（被引用数・H-index・G-index）― 分野水準と比較して
3. 掲載ジャーナルの質（分野内での評価・Impact Factor水準・Q1/Q2/Q3/Q4の傾向）
4. 評価期間における最近の活動度
5. 研究分野・専門性の特徴と独自性
6. 研究資金獲得力（科研費の代表/分担比率、種目ステップアップ、総額、ファンディングの空白、独立PIとしての確立度）― 分野・キャリア段階に照らして
7. 論文業績と科研費獲得実績の整合性（業績が大型科研につながっているか、または科研費の規模が業績規模と釣り合っているか）
8. 総合評価コメント（強み・改善点・今後の期待）"""

    extra_section = ""
    if extra_instructions and extra_instructions.strip():
        extra_section = f"\n【評価の観点・追加指示(ユーザ指定)】\n{extra_instructions.strip()}\n"

    return f"""以下は、ある研究者の業績データです。
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
{kaken_section}
上記データをもとに、推定された研究分野の慣例・水準と照らし合わせながら、以下の観点で忖度なく研究者を総合評価してください：
{eval_points}
{extra_section}
分野の違いによるバイアスを補正した上で、具体的かつ建設的に{lang}で記述してください。
"""


def evaluate_achievements(papers, report, lang="ja", grants=None,
                          extra_instructions=None, model=DEFAULT_MODEL):
    """業績指標と論文リストに基づいて AI が研究者を総合評価する(非ストリーミング)。"""
    key = llm.required_key_for(model)
    if not os.getenv(key):
        return f"{key} not found. Skipping evaluation."

    field_ctx = _infer_field_context(model, [p["title"] for p in papers])
    prompt = _build_eval_prompt(papers, report, lang, grants, field_ctx,
                                extra_instructions=extra_instructions)
    return llm.complete(model, prompt)


def evaluate_achievements_stream(papers, report, lang="ja", grants=None,
                                 field_ctx=None, extra_instructions=None,
                                 model=DEFAULT_MODEL):
    """`evaluate_achievements` のストリーミング版。累積テキストを順次 yield する。

    field_ctx を渡すと内部の `_infer_field_context` 呼び出しを省略できる
    (WebUI 側で先に推定して保存している場合の最適化)。
    extra_instructions はユーザが UI に入力した自由形式の評価観点・追加指示。
    model でプロバイダを選択(既定: gpt-5.4)。
    """
    key = llm.required_key_for(model)
    if not os.getenv(key):
        yield f"{key} not found. Skipping evaluation."
        return

    if field_ctx is None:
        field_ctx = _infer_field_context(model, [p["title"] for p in papers])
    prompt = _build_eval_prompt(papers, report, lang, grants, field_ctx,
                                extra_instructions=extra_instructions)

    last = ""
    for partial in llm.stream(model, prompt):
        last = partial
        yield partial
    if not last:
        yield ""


# ---------------------------------------------------------------------------
# Researcher comparison (人事選考用)
# ---------------------------------------------------------------------------

def _build_compare_prompt(researchers_with_fields, lang="ja", extra_instructions=None):
    """複数候補の業績データを 1 つの比較プロンプトにまとめる。

    researchers_with_fields の各要素は dict:
      { name, report, papers, grants?, field_ctx? }
    extra_instructions に文字列を渡すと「【評価の観点・追加指示】」セクションが
    比較観点の後に挿入される。
    """
    sections = []
    for i, r in enumerate(researchers_with_fields, start=1):
        name = r.get("name") or f"候補{i}"
        report = r.get("report") or {}
        papers = r.get("papers") or []
        grants = r.get("grants") or []
        field = r.get("field_ctx") or {}

        # トップ論文 5 件
        top_papers = sorted(papers, key=lambda x: x.get("citations", 0), reverse=True)[:5]
        top_titles = "\n".join(
            f"    - {p.get('title','')} [{p.get('journal') or '不明'}] "
            f"(citations: {p.get('citations',0)}, year: {p.get('year','-')})"
            for p in top_papers
        ) or "    (論文なし)"

        # ジャーナル要約(上位 5 誌)
        journal_summary = _build_journal_summary(papers)
        journal_lines = "\n".join(
            f"    - {j['journal']}: {j['count']} 件, 総被引用 {j['citations']} 件"
            for j in journal_summary[:5]
        ) or "    (なし)"

        # KAKEN 要約
        ksum = _build_kaken_summary(grants) if grants else None
        if ksum:
            kaken_lines = (
                f"  - KAKEN: {ksum['total_count']} 件"
                f"(代表 {ksum['pi_count']} / 分担 {ksum['coi_count']}), "
                f"総額 {ksum['total_sum']:,} 円"
                + (f", ファンディング {ksum['first_year']}〜{ksum['last_year']} 年度"
                   if ksum['first_year'] and ksum['last_year'] else "")
            )
        else:
            kaken_lines = "  - KAKEN: 未取得 / 該当なし"

        # 分野
        if field:
            sub = ", ".join(field.get("sub_fields", []))
            field_lines = (
                f"  - 推定分野: {field.get('field','不明')}"
                + (f" / {sub}" if sub else "")
                + f"\n  - 分野水準: 被引用 {field.get('citation_norm','-')}, "
                f"H-index {field.get('hindex_norm','-')}, "
                f"年間発表 {field.get('pub_rate_norm','-')}"
                + (f"\n  - 分野の注意: {field.get('notes','')}" if field.get("notes") else "")
            )
        else:
            field_lines = "  - 推定分野: 不明(分野補正なし)"

        sections.append(f"""【候補 {i}: {name}】
{field_lines}
  - 統計: 総論文 {report.get('total_count',0)} 件, 総被引用 {report.get('total_citations',0)} 件, """
            f"H-index {report.get('h_index',0)}, G-index {report.get('g_index',0)}, "
            f"研究歴 {report.get('research_years','-')} 年 (開始 {report.get('start_year','-')})\n"
            f"  - 評価期間内: 論文 {report.get('recent_count',0)} 件, "
            f"被引用 {report.get('recent_citations',0)} 件, "
            f"筆頭 {report.get('recent_first_author',0)} 件\n"
            f"{kaken_lines}\n"
            f"  - 主要ジャーナル(被引用順):\n{journal_lines}\n"
            f"  - 被引用上位論文:\n{top_titles}"
        )

    candidates_text = "\n\n".join(sections)
    extra_section = ""
    if extra_instructions and extra_instructions.strip():
        extra_section = (
            "\n【評価の観点・追加指示(ユーザ指定)】\n"
            f"{extra_instructions.strip()}\n"
        )
    return f"""以下は人事選考の候補者 {len(researchers_with_fields)} 名の業績データです。
各候補の **推定研究分野** とその **分野水準** を踏まえ、分野バイアスを補正したうえで
比較評価してください。

{candidates_text}

【比較・評価の観点】
1. 各候補の研究分野とその分野水準に照らした業績水準
   - 同じ分野の候補同士は直接比較してよい
   - 異なる分野の候補は、分野水準(被引用数の目安・H-index 目安・発表頻度)で補正したうえで判断
2. 生産性(論文数・継続性) / 影響力(被引用数・H-index・G-index)
3. 掲載ジャーナルの質(Q1/Q2 など)
4. 評価期間における最近の活動度
5. 研究資金獲得力(科研費の代表/分担比率、種目ステップアップ、総額)
6. 候補ごとの **強み** と **懸念**
7. **総合ランキング**(タイ可)と **人事選考の観点での推薦コメント**
{extra_section}
応答は{lang}で、Markdown の見出し(##, ###)で構造化してください。
ランキングは最後にまとめてください。"""


def compare_researchers_stream(researchers_data, lang="ja",
                               extra_instructions=None, model=DEFAULT_MODEL):
    """複数研究者の比較評価をストリーミング出力するジェネレータ。

    researchers_data の各要素は dict:
      { name, report, papers, grants?, field_ctx? }
    field_ctx が無い場合はこの関数内で _infer_field_context を呼んで補完する。
    extra_instructions はユーザが UI に入力した評価の観点・追加指示。
    model でプロバイダを選択(既定: gpt-5.4)。

    yield: 累積テキスト(str)。
    """
    key = llm.required_key_for(model)
    if not os.getenv(key):
        yield f"{key} not found. Skipping evaluation."
        return

    if not researchers_data:
        yield ""
        return

    # 各候補の field_ctx を埋める(無ければ推定)
    enriched = []
    for r in researchers_data:
        r_copy = dict(r)
        if not r_copy.get("field_ctx"):
            titles = [p.get("title", "") for p in (r_copy.get("papers") or [])]
            r_copy["field_ctx"] = _infer_field_context(model, titles) if titles else {}
        enriched.append(r_copy)

    prompt = _build_compare_prompt(enriched, lang=lang,
                                   extra_instructions=extra_instructions)
    last = ""
    for partial in llm.stream(model, prompt, max_tokens=16384):
        last = partial
        yield partial
    if not last:
        yield ""