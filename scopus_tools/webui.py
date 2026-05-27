"""Gradio Web UI for scopus-tools.

階層: Project(部局・グループ等の単位)→ Researcher(個人)→ Scopus/KAKEN/AI 結果。

レイアウト:
  - 左サイドバー:
      上段: Project Dropdown + 新規/リネーム/削除
      下段: 選択中プロジェクト内の Researcher Radio + 新規/リネーム/削除
  - 右メイン: 3 タブ(Scopus / KAKEN / AI 評価)

各「実行」完了時に研究者のセクションが自動セーブされる。

依存: gradio>=4.0 (オプション extras: pip install -e ".[ui]")
起動: scopus-tools webui [--projects-dir PATH]
"""

import contextlib
import io
import logging
import os
import re

from scopus_tools import llm
from scopus_tools.core import default_eval_year_range
from scopus_tools.projects import (
    ProjectStore, add_researcher, completion_flags, empty_project,
    find_researcher, make_unique_researcher_name, merge_researcher_section,
    remove_researcher, rename_researcher, set_project_comparison,
)

logger = logging.getLogger(__name__)

_KAKEN_ID_RE = re.compile(r"\b\d{8}\b")


# ---------------------------------------------------------------------------
# Clipboard / Download JS
# ---------------------------------------------------------------------------

_COPY_JS = """
(text) => {
  if (!text) { return []; }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(err => console.warn("clipboard error:", err));
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { console.warn(e); }
    document.body.removeChild(ta);
  }
  return [];
}
"""


def _download_js(prefix):
    return (
        "(text) => {"
        "  if (!text) { return []; }"
        "  const blob = new Blob([text], {type: 'text/markdown;charset=utf-8'});"
        "  const url = URL.createObjectURL(blob);"
        "  const a = document.createElement('a');"
        "  a.href = url;"
        f"  const prefix = '{prefix}';"
        "  const ts = new Date().toISOString().slice(0,19).replace(/[:\\-T]/g, '').replace(/\\..*/, '');"
        "  a.download = prefix + '_' + ts + '.md';"
        "  document.body.appendChild(a);"
        "  a.click();"
        "  document.body.removeChild(a);"
        "  URL.revokeObjectURL(url);"
        "  return [];"
        "}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_keys():
    """(scopus_ok, ai_ok, kaken_ok, openai_ok, anthropic_ok) を返す。

    ai_ok は OpenAI / Anthropic のどちらか一方でも設定されていれば True。
    """
    openai_ok = bool(os.getenv("OPENAI_API_KEY"))
    anthropic_ok = bool(os.getenv("ANTHROPIC_API_KEY"))
    return (
        bool(os.getenv("SCOPUS_API_KEY")),
        openai_ok or anthropic_ok,
        bool(os.getenv("KAKEN_APP_ID")),
        openai_ok,
        anthropic_ok,
    )


def _format_scopus_candidate(author):
    return (
        f"{author.get('name', '')} | "
        f"{author.get('affiliation') or '所属情報なし'} | "
        f"{author.get('doc_count', '')} docs | "
        f"Scopus ID: {author['id']}"
    )


def _format_kaken_candidate(r):
    parts = [
        r.get("name") or "(氏名不明)",
        r.get("affiliation") or "所属情報なし",
    ]
    if r.get("job_title"):
        parts.append(r["job_title"])
    if r.get("project_count"):
        parts.append(f"{r['project_count']} projects")
    parts.append(f"研究者番号: {r.get('researcher_id', '')}")
    return " | ".join(parts)


def _parse_manual_kaken_ids(text):
    if not text:
        return []
    return _KAKEN_ID_RE.findall(text)


def _project_choices(store):
    """Project Dropdown 用の (label, name) リスト。"""
    out = []
    for p in store.list():
        n = p["researcher_count"]
        out.append((f"{p['name']}  ({n} 研究者)", p["name"]))
    return out


def _researcher_choices(project):
    """Researcher Radio 用の (label, name) リスト(完了アイコン付き)。"""
    out = []
    for r in project.get("researchers") or []:
        s, k, a = completion_flags(r)
        flag_str = (
            ("S✓" if s else "S✗")
            + ("K✓" if k else "K✗")
            + ("A✓" if a else "A✗")
        )
        out.append((f"{r.get('name','')}  {flag_str}", r.get("name", "")))
    return out


def _save_researcher_section(store, project_name, researcher_name, section, partial):
    """指定プロジェクトの指定研究者の section を更新して保存。"""
    if not project_name or not researcher_name:
        return
    project = store.load(project_name) or empty_project(project_name)
    merge_researcher_section(project, researcher_name, section, partial)
    store.save(project_name, project)


def _ai_status_md(scopus_state, kaken_state):
    lines = []
    if scopus_state and scopus_state.get("papers"):
        n = len(scopus_state["papers"])
        ys, ye = scopus_state.get("year_range", (None, None))
        name = f"{scopus_state.get('first') or ''} {scopus_state.get('last') or ''}".strip()
        ids = ", ".join(scopus_state.get("scopus_ids") or [])
        lines.append(f"✅ **Scopus**: {name} ({ids}) / 論文 {n} 件 / 評価期間 {ys}–{ye}")
    else:
        lines.append("⚠️ **Scopus**: 未取得。Scopus タブで「Scopus 集計を実行」してください")

    if kaken_state and kaken_state.get("grants") is not None:
        n = len(kaken_state["grants"])
        ids = ", ".join(kaken_state.get("kaken_ids") or [])
        lines.append(f"✅ **KAKEN**: {ids} / 課題 {n} 件")
    else:
        lines.append("ℹ️ **KAKEN**: 未取得(任意 — KAKEN タブで集計するとプロンプトに含まれます)")

    return "\n\n".join(lines)


def _env_banner():
    scopus_ok, ai_ok, kaken_ok, openai_ok, anthropic_ok = _check_keys()
    lines = []
    if not scopus_ok:
        lines.append("⚠️ `SCOPUS_API_KEY` が未設定です。Scopus パネルが使えません。")
    if not ai_ok:
        lines.append(
            "ℹ️ `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` のいずれも未設定です。"
            "AI 評価タブが使えません。"
        )
    elif not openai_ok:
        lines.append("ℹ️ `OPENAI_API_KEY` 未設定 — `gpt-*` モデルは選べません。")
    elif not anthropic_ok:
        lines.append("ℹ️ `ANTHROPIC_API_KEY` 未設定 — `claude-*` モデルは選べません。")
    if not kaken_ok:
        lines.append("ℹ️ `KAKEN_APP_ID` が未設定です。KAKEN タブが使えません。")
    return "\n".join(lines) or (
        "✅ Scopus / OpenAI / Anthropic / KAKEN の API キーが揃っています。"
    )


# ---------------------------------------------------------------------------
# Tab handlers
# ---------------------------------------------------------------------------

def _scopus_search(store, project_name, researcher_name, name, progress):
    import gradio as gr
    from scopus_tools import api

    if not name or not name.strip():
        return gr.update(choices=[], value=[], visible=False), "⚠️ 研究者名を入力してください"

    scopus_ok, *_ = _check_keys()
    if not scopus_ok:
        return (
            gr.update(choices=[], value=[], visible=False),
            "❌ `SCOPUS_API_KEY` が未設定です。`.env` を確認してください。",
        )

    progress(0.5, desc="Scopus 著者検索中…")
    try:
        client = api.ScopusClient()
        results = client.search_author_by_name(name)
    except Exception as e:  # noqa: BLE001
        logger.exception("Scopus search failed")
        return (
            gr.update(choices=[], value=[], visible=False),
            f"❌ Scopus 検索でエラーが発生しました: {e}",
        )

    if not results:
        _save_researcher_section(store, project_name, researcher_name, "scopus", {
            "query": name, "candidates": [],
        })
        return (
            gr.update(choices=[], value=[], visible=True),
            f"❌ '{name}' の候補は見つかりませんでした",
        )

    choices = [(_format_scopus_candidate(r), r["id"]) for r in results]
    _save_researcher_section(store, project_name, researcher_name, "scopus", {
        "query": name,
        "candidates": [
            {"id": r["id"], "name": r.get("name"),
             "affiliation": r.get("affiliation"),
             "doc_count": r.get("doc_count")}
            for r in results
        ],
    })
    return (
        gr.update(choices=choices, value=[], visible=True),
        f"✅ {len(results)} 件ヒット。該当者をチェック(同一人物が複数 ID に分裂している場合は全て ON)",
    )


def _scopus_run(store, project_name, researcher_name, scopus_ids, year_start, year_end, progress):
    from scopus_tools import api, core, utils

    if isinstance(scopus_ids, str):
        scopus_ids = [scopus_ids] if scopus_ids else []
    scopus_ids = [str(s) for s in (scopus_ids or []) if s]
    if not scopus_ids:
        return None, "", "⚠️ Scopus 候補を 1 件以上チェックしてください", _ai_status_md(None, None)

    try:
        year_range = (int(year_start), int(year_end))
    except (TypeError, ValueError):
        return None, "", "❌ 年範囲が不正です(整数で指定してください)", _ai_status_md(None, None)
    if year_range[0] > year_range[1]:
        return None, "", "❌ 開始年は終了年以下にしてください", _ai_status_md(None, None)

    scopus_ok, *_ = _check_keys()
    if not scopus_ok:
        return None, "", "❌ `SCOPUS_API_KEY` が未設定です", _ai_status_md(None, None)

    progress(0.2, desc=f"Scopus から論文を取得中…({len(scopus_ids)} ID 統合)")
    try:
        client = api.ScopusClient()
        papers = client.search_papers(scopus_ids)
        first, last = client.get_author_profile(scopus_ids[0])
    except Exception as e:  # noqa: BLE001
        logger.exception("Scopus fetch failed")
        return None, "", f"❌ Scopus 取得でエラーが発生しました: {e}", _ai_status_md(None, None)

    report = core.summarize_papers(papers, year_range=year_range)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        utils.print_report_text(first, last, scopus_ids, report, papers, year_range=year_range)
    biblio_md = f"```\n{buf.getvalue()}\n```"

    state = {
        "scopus_ids": scopus_ids,
        "first": first, "last": last,
        "papers": papers, "report": report,
        "year_range": year_range,
    }
    plural = f" ({len(scopus_ids)} ID 統合)" if len(scopus_ids) > 1 else ""
    status = (
        f"✅ {first or ''} {last or ''}".strip()
        + f" の Scopus 集計が完了しました{plural} — AI 評価タブで使えます"
    )

    _save_researcher_section(store, project_name, researcher_name, "scopus", {
        "selected_ids": scopus_ids,
        "first": first, "last": last,
        "papers": papers, "report": report,
        "year_range": list(year_range),
        "biblio_md": biblio_md,
        "run_status": status,
    })
    return state, biblio_md, status, _ai_status_md(state, None)


def _kaken_search(store, project_name, researcher_name, name, progress):
    import gradio as gr
    from scopus_tools import kaken as kaken_mod

    _, _, kaken_ok, _, _ = _check_keys()
    if not kaken_ok:
        return (
            gr.update(choices=[], value=[], visible=False),
            "❌ `KAKEN_APP_ID` が未設定です。`.env` を確認してください。",
        )
    if not name or not name.strip():
        return gr.update(choices=[], value=[], visible=False), "⚠️ 研究者名を入力してください"

    progress(0.5, desc="KAKEN 研究者検索中…")
    try:
        kc = kaken_mod.KakenClient()
        results = kc.search_researcher_by_name(name, lang="ja")
    except Exception as e:  # noqa: BLE001
        logger.exception("KAKEN search failed")
        return (
            gr.update(choices=[], value=[], visible=True),
            f"⚠️ KAKEN 検索でエラー: {e}",
        )

    if not results:
        _save_researcher_section(store, project_name, researcher_name, "kaken",
                                 {"query": name, "candidates": []})
        return (
            gr.update(choices=[], value=[], visible=True),
            "❌ 候補なし。下のテキストボックスで 8 桁研究者番号を直接指定できます",
        )

    choices = [(_format_kaken_candidate(r), r["researcher_id"]) for r in results]
    _save_researcher_section(store, project_name, researcher_name, "kaken", {
        "query": name,
        "candidates": [
            {"researcher_id": r.get("researcher_id"),
             "name": r.get("name"),
             "affiliation": r.get("affiliation"),
             "job_title": r.get("job_title"),
             "project_count": r.get("project_count")}
            for r in results
        ],
    })
    return (
        gr.update(choices=choices, value=[], visible=True),
        f"✅ {len(results)} 件ヒット。該当者をチェック",
    )


def _kaken_run(store, project_name, researcher_name, kaken_selected_ids, kaken_manual_text, progress):
    from scopus_tools import kaken as kaken_mod, utils

    _, _, kaken_ok, _, _ = _check_keys()
    if not kaken_ok:
        return None, "", "❌ `KAKEN_APP_ID` が未設定です", None

    kaken_selected_ids = [str(k) for k in (kaken_selected_ids or []) if k]
    manual_ids = _parse_manual_kaken_ids(kaken_manual_text or "")
    seen = set()
    kaken_ids = []
    for k in kaken_selected_ids + manual_ids:
        if k not in seen:
            seen.add(k)
            kaken_ids.append(k)
    if not kaken_ids:
        return None, "", "⚠️ KAKEN 候補をチェックするか研究者番号を直接入力してください", None

    progress(0.3, desc=f"KAKEN 課題を取得中…({len(kaken_ids)} 研究者番号)")
    try:
        kc = kaken_mod.KakenClient()
        grants = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for rid in kaken_ids:
                r_grants = kc.get_grants_by_researcher_id(rid, lang="ja")
                grants.extend(r_grants)
                researcher = kc.search_researcher_by_id(rid, lang="ja")
                utils.print_kaken_summary(rid, researcher, r_grants)
                print()
    except Exception as e:  # noqa: BLE001
        logger.exception("KAKEN error")
        return None, "", f"⚠️ KAKEN 連携でエラー: {e}", None

    kaken_md = (
        f"**指定された研究者番号: {', '.join(kaken_ids)} (合計 {len(grants)} 課題)**\n\n"
        f"```\n{buf.getvalue()}\n```"
    )
    state = {"kaken_ids": kaken_ids, "grants": grants}
    status = f"✅ KAKEN 集計が完了しました(課題 {len(grants)} 件) — AI 評価タブで使えます"

    _save_researcher_section(store, project_name, researcher_name, "kaken", {
        "selected_ids": kaken_selected_ids,
        "manual_text": kaken_manual_text or "",
        "kaken_ids": kaken_ids,
        "grants": grants,
        "kaken_md": kaken_md,
        "run_status": status,
    })
    return state, kaken_md, status, state


def _ai_run(store, project_name, researcher_name, scopus_state, kaken_state, lang,
            extra_instructions, model, progress):
    from scopus_tools import ai_engine

    key = llm.required_key_for(model)
    if not os.getenv(key):
        yield "", f"❌ `{key}` が未設定です(モデル {model} に必要)"
        return
    if not scopus_state or not scopus_state.get("papers"):
        yield "", "⚠️ 先に Scopus タブで集計を実行してください"
        return

    papers = scopus_state["papers"]
    report = scopus_state["report"]
    grants = (kaken_state or {}).get("grants")
    n_papers = len(papers)
    n_grants = len(grants) if grants else 0
    src = f"論文 {n_papers} 件" + (f" + KAKEN 課題 {n_grants} 件" if n_grants else "")

    progress(0.1, desc="分野推定中…(数秒)")
    yield "", f"🔄 分野推定中…({src} を入力に使用)"

    # 先に分野推定を済ませて researcher.ai.field_ctx に保存
    # → 比較タブの「推定分野」列で再利用できる
    field_ctx = None
    try:
        field_ctx = ai_engine.infer_field(papers, model=model)
        if field_ctx:
            _save_researcher_section(store, project_name, researcher_name, "ai", {
                "field_ctx": field_ctx,
            })
    except Exception:  # noqa: BLE001
        logger.exception("Field inference failed (continuing without)")

    progress(0.4, desc=f"AI 評価を生成中({model}, ストリーミング)…")
    last = ""
    err_status = None
    try:
        for partial in ai_engine.evaluate_achievements_stream(
            papers, report, lang=lang or "ja", grants=grants,
            field_ctx=field_ctx,
            extra_instructions=extra_instructions,
            model=model,
        ):
            last = partial
            yield partial, f"⏳ 生成中…({len(partial)} 文字, model={model})"
    except Exception as e:  # noqa: BLE001
        logger.exception("AI error")
        err_status = f"⚠️ AI 評価でエラー: {e}"

    if err_status:
        _save_researcher_section(store, project_name, researcher_name, "ai", {
            "lang": lang or "ja",
            "model": model,
            "evaluation": last,
            "run_status": err_status,
            "extra_instructions": extra_instructions or "",
        })
        yield last, err_status
        return

    final_status = (
        f"✅ AI 評価が完了しました({src} を入力に使用 / {len(last)} 文字 / model={model})"
    )
    _save_researcher_section(store, project_name, researcher_name, "ai", {
        "lang": lang or "ja",
        "model": model,
        "evaluation": last,
        "run_status": final_status,
        "extra_instructions": extra_instructions or "",
    })
    yield last, final_status


# ---------------------------------------------------------------------------
# Comparison tab helpers (人事選考用)
# ---------------------------------------------------------------------------

_COMPARE_COLUMNS = [
    "研究者", "推定分野", "AI評価",
    "研究歴", "開始年",
    "総論文", "総被引用", "H", "G",
    "評価期間", "期間内論文", "期間内被引用", "期間内筆頭",
    "KAKEN件", "KAKEN代表", "KAKEN総額",
]


def _fmt_int(v, default="—"):
    """整数なら 3 桁区切り文字列に。それ以外は default。"""
    if v is None or v == "":
        return default
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v) if v else default


def _row_for_researcher(researcher):
    """1 研究者ぶんの比較表の行(list[str])を返す。全要素文字列で揃える。未取得は '—'。"""
    name = researcher.get("name") or "(無名)"
    s = researcher.get("scopus") or {}
    k = researcher.get("kaken") or {}
    a = researcher.get("ai") or {}
    report = s.get("report") or {}
    year_range = s.get("year_range") or []
    year_str = f"{year_range[0]}–{year_range[1]}" if len(year_range) == 2 else "—"
    grants = k.get("grants") or []
    has_kaken_section = bool(k)
    pi_count = sum(1 for g in grants if g.get("my_role") == "principal_investigator")
    total_cost = sum(
        int(g.get("total_cost") or 0)
        for g in grants
        if str(g.get("total_cost") or "").isdigit()
    )

    # 推定分野(個別 AI 評価で保存された field_ctx があれば優先)
    field_ctx = a.get("field_ctx") or {}
    field_name = field_ctx.get("field") or "—"

    ai_done = "✓" if (a.get("evaluation") or "").strip() else "—"

    def _fmt_yen(v):
        try:
            return f"{int(v):,} 円"
        except (TypeError, ValueError):
            return "—"

    return [
        str(name),
        str(field_name),
        ai_done,
        _fmt_int(report.get("research_years")),
        _fmt_int(report.get("start_year")),
        _fmt_int(report.get("total_count")),
        _fmt_int(report.get("total_citations")),
        _fmt_int(report.get("h_index")),
        _fmt_int(report.get("g_index")),
        year_str,
        _fmt_int(report.get("recent_count")),
        _fmt_int(report.get("recent_citations")),
        _fmt_int(report.get("recent_first_author")),
        str(len(grants)) if has_kaken_section else "—",
        str(pi_count) if has_kaken_section else "—",
        _fmt_yen(total_cost) if grants else "—",
    ]


def _table_to_markdown(rows):
    """List-of-list → GitHub-flavored Markdown 表。"""
    header = "| " + " | ".join(_COMPARE_COLUMNS) + " |"
    sep = "|" + "|".join(["---"] * len(_COMPARE_COLUMNS)) + "|"
    body = "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
    return f"{header}\n{sep}\n{body}"


def _build_comparison_table(store, project_name, selected_names):
    """選択された研究者の比較表を組み立てる。

    戻り値: (rows_list_of_list, markdown_str, status_md)
    """
    empty_rows = []
    if not project_name:
        return empty_rows, "", "⚠️ プロジェクトが選択されていません"
    if not selected_names:
        return empty_rows, "", "⚠️ 比較対象の研究者を 1 名以上チェックしてください"

    project = store.load(project_name) or empty_project(project_name)
    rows = []
    for name in selected_names:
        r = find_researcher(project, name)
        if r is None:
            continue
        rows.append(_row_for_researcher(r))
    if not rows:
        return empty_rows, "", "❌ 選択された研究者がプロジェクトに見つかりません"

    md = _table_to_markdown(rows)
    # 保存
    project_data = store.load(project_name) or empty_project(project_name)
    existing = project_data.get("comparison") or {}
    set_project_comparison(project_data, {
        **existing,
        "selected_names": list(selected_names),
        "table_md": md,
    })
    store.save(project_name, project_data)
    return rows, md, f"✅ {len(rows)} 名の比較表を生成しました"


def _collect_researchers_for_ai(project, selected_names):
    """AI 比較評価に渡す per-researcher dict のリストを組み立てる。

    Scopus(papers/report)が無い研究者はスキップして警告。
    """
    items = []
    skipped = []
    for name in selected_names:
        r = find_researcher(project, name)
        if r is None:
            continue
        s = r.get("scopus") or {}
        if not s.get("papers"):
            skipped.append(name)
            continue
        k = r.get("kaken") or {}
        items.append({
            "name": name,
            "report": s.get("report") or {},
            "papers": s.get("papers") or [],
            "grants": k.get("grants") or [],
            "field_ctx": (r.get("ai") or {}).get("field_ctx"),  # 個別評価で保存していれば再利用
        })
    return items, skipped


def _compare_run(store, project_name, selected_names, lang, extra_instructions, model, progress):
    """AI 比較評価ストリーミングジェネレータ。各 yield ごとに (ai_md, status_md) を返す。"""
    from scopus_tools import ai_engine

    key = llm.required_key_for(model)
    if not os.getenv(key):
        yield "", f"❌ `{key}` が未設定です(モデル {model} に必要)"
        return
    if not project_name:
        yield "", "⚠️ プロジェクトが選択されていません"
        return
    if not selected_names:
        yield "", "⚠️ 比較対象の研究者を 1 名以上チェックしてください"
        return
    if len(selected_names) > 10:
        yield "", f"❌ 件数が多すぎます({len(selected_names)} 名)。10 名以下にしてください"
        return

    project = store.load(project_name) or empty_project(project_name)
    items, skipped = _collect_researchers_for_ai(project, selected_names)
    if not items:
        yield "", "❌ Scopus 集計済みの研究者がいません。先に各人の Scopus タブで集計してください"
        return

    notice = f"({len(items)} 名, model={model})"
    if skipped:
        notice += f" / スキップ: {', '.join(skipped)}(Scopus 未取得)"

    progress(0.1, desc="各候補の分野推定中…(数秒×人数)")
    yield "", f"🔄 分野推定中… {notice}"

    progress(0.4, desc=f"AI 比較評価を生成中({model}, ストリーミング)…")
    last = ""
    err_status = None
    try:
        for partial in ai_engine.compare_researchers_stream(
            items, lang=lang or "ja", extra_instructions=extra_instructions, model=model,
        ):
            last = partial
            yield partial, f"⏳ 生成中…({len(partial)} 文字) {notice}"
    except Exception as e:  # noqa: BLE001
        logger.exception("Compare AI error")
        err_status = f"⚠️ 比較評価でエラー: {e}"

    def _save(final_eval):
        project_data = store.load(project_name) or empty_project(project_name)
        existing = project_data.get("comparison") or {}
        set_project_comparison(project_data, {
            **existing,
            "selected_names": list(selected_names),
            "lang": lang or "ja",
            "model": model,
            "ai_evaluation": final_eval,
            "extra_instructions": extra_instructions or "",
        })
        store.save(project_name, project_data)

    if err_status:
        _save(last)
        yield last, err_status
        return

    final_status = f"✅ AI 比較評価が完了しました {notice} / {len(last)} 文字"
    _save(last)
    yield last, final_status


# ---------------------------------------------------------------------------
# Right pane update helpers
# ---------------------------------------------------------------------------

_RIGHT_OUTPUT_KEYS = [
    "s_name", "s_search_status", "s_select",
    "s_year_start", "s_year_end", "s_run_status", "s_result",
    "k_name", "k_search_status", "k_select", "k_manual",
    "k_run_status", "k_result",
    "ai_status_display", "ai_lang", "ai_model", "ai_extra",
    "ai_run_status", "ai_result",
    "scopus_state", "kaken_state",
]

_COMPARE_OUTPUT_KEYS = [
    "compare_select", "compare_extra", "compare_model",
    "compare_table", "compare_table_md_state",
    "compare_table_status", "compare_ai_result", "compare_ai_status",
]


def _compare_pane_empty():
    import gradio as gr
    return {
        "compare_select": gr.update(choices=[], value=[]),
        "compare_extra": gr.update(value=""),
        "compare_model": gr.update(value=llm.DEFAULT_MODEL),
        "compare_table": gr.update(value=[]),
        "compare_table_md_state": "",
        "compare_table_status": gr.update(value=""),
        "compare_ai_result": gr.update(value=""),
        "compare_ai_status": gr.update(value=""),
    }


def _compare_pane_from_project(project):
    """プロジェクトから比較タブの全コンポーネント更新マップを組む。"""
    import gradio as gr
    if not project:
        return _compare_pane_empty()
    names = [r.get("name") for r in (project.get("researchers") or []) if r.get("name")]
    choices = [(name, name) for name in names]

    comparison = project.get("comparison") or {}
    saved_selected = comparison.get("selected_names") or []
    selected = [s for s in saved_selected if s in names]  # 削除済み研究者をフィルタ
    table_md = comparison.get("table_md", "")
    ai_eval = comparison.get("ai_evaluation", "")

    rows = []
    if selected:
        for name in selected:
            r = find_researcher(project, name)
            if r is not None:
                rows.append(_row_for_researcher(r))

    return {
        "compare_select": gr.update(choices=choices, value=selected),
        "compare_extra": gr.update(value=comparison.get("extra_instructions", "")),
        "compare_model": gr.update(value=comparison.get("model", llm.DEFAULT_MODEL)),
        "compare_table": gr.update(value=rows),
        "compare_table_md_state": table_md,
        "compare_table_status": gr.update(value=""),
        "compare_ai_result": gr.update(value=ai_eval),
        "compare_ai_status": gr.update(value=""),
    }


def _compare_select_refresh(project):
    """研究者一覧変更時に compare_select の choices だけ更新(value は維持)。"""
    import gradio as gr
    if not project:
        return gr.update(choices=[], value=[])
    names = [r.get("name") for r in (project.get("researchers") or []) if r.get("name")]
    return gr.update(choices=[(n, n) for n in names])


def _empty_right_pane_updates(researcher_name=None):
    """右ペインをリセットする gr.update 群を返す。

    researcher_name が指定されたら、Scopus/KAKEN 検索欄(s_name / k_name)に
    その名前を初期値として入れる(研究者を選んだ直後に検索しやすくする)。
    """
    import gradio as gr
    default_name = (researcher_name or "").strip()
    default_start, default_end = default_eval_year_range()
    return {
        "s_name": gr.update(value=default_name),
        "s_search_status": gr.update(value=""),
        "s_select": gr.update(choices=[], value=[], visible=False),
        "s_year_start": gr.update(value=default_start),
        "s_year_end": gr.update(value=default_end),
        "s_run_status": gr.update(value=""),
        "s_result": gr.update(value=""),
        "k_name": gr.update(value=default_name),
        "k_search_status": gr.update(value=""),
        "k_select": gr.update(choices=[], value=[], visible=False),
        "k_manual": gr.update(value=""),
        "k_run_status": gr.update(value=""),
        "k_result": gr.update(value=""),
        "ai_status_display": gr.update(value=_ai_status_md(None, None)),
        "ai_lang": gr.update(value="ja"),
        "ai_model": gr.update(value=llm.DEFAULT_MODEL),
        "ai_extra": gr.update(value=""),
        "ai_run_status": gr.update(value=""),
        "ai_result": gr.update(value=""),
        "scopus_state": None,
        "kaken_state": None,
    }


def _researcher_to_updates(researcher):
    import gradio as gr
    researcher_name = researcher.get("name") if researcher else None
    updates = _empty_right_pane_updates(researcher_name=researcher_name)
    if not researcher:
        return updates

    s = researcher.get("scopus") or {}
    if s:
        # 過去に検索したクエリがあればそれを優先、無ければ研究者名を残す
        updates["s_name"] = gr.update(value=s.get("query") or (researcher_name or ""))
        cands = s.get("candidates") or []
        choices = [(_format_scopus_candidate(c), c["id"]) for c in cands if c.get("id")]
        updates["s_select"] = gr.update(
            choices=choices,
            value=s.get("selected_ids") or [],
            visible=bool(choices),
        )
        if s.get("year_range"):
            ys, ye = s["year_range"]
            updates["s_year_start"] = gr.update(value=ys)
            updates["s_year_end"] = gr.update(value=ye)
        updates["s_result"] = gr.update(value=s.get("biblio_md", ""))
        updates["s_run_status"] = gr.update(value=s.get("run_status", ""))
        if s.get("papers"):
            updates["scopus_state"] = {
                "scopus_ids": s.get("selected_ids") or [],
                "first": s.get("first"), "last": s.get("last"),
                "papers": s.get("papers") or [],
                "report": s.get("report") or {},
                "year_range": tuple(s.get("year_range") or (2020, 2025)),
            }

    k = researcher.get("kaken") or {}
    if k:
        updates["k_name"] = gr.update(value=k.get("query") or (researcher_name or ""))
        cands = k.get("candidates") or []
        choices = [
            (_format_kaken_candidate(c), c["researcher_id"])
            for c in cands if c.get("researcher_id")
        ]
        updates["k_select"] = gr.update(
            choices=choices,
            value=k.get("selected_ids") or [],
            visible=bool(choices),
        )
        updates["k_manual"] = gr.update(value=k.get("manual_text", ""))
        updates["k_result"] = gr.update(value=k.get("kaken_md", ""))
        updates["k_run_status"] = gr.update(value=k.get("run_status", ""))
        if k.get("grants") is not None:
            updates["kaken_state"] = {
                "kaken_ids": k.get("kaken_ids") or [],
                "grants": k.get("grants") or [],
            }

    a = researcher.get("ai") or {}
    if a:
        updates["ai_lang"] = gr.update(value=a.get("lang", "ja"))
        updates["ai_model"] = gr.update(value=a.get("model", llm.DEFAULT_MODEL))
        updates["ai_extra"] = gr.update(value=a.get("extra_instructions", ""))
        updates["ai_result"] = gr.update(value=a.get("evaluation", ""))
        updates["ai_run_status"] = gr.update(value=a.get("run_status", ""))

    updates["ai_status_display"] = gr.update(
        value=_ai_status_md(updates["scopus_state"], updates["kaken_state"])
    )
    return updates


def _right_tuple_from(updates):
    return tuple(updates[k] for k in _RIGHT_OUTPUT_KEYS)


def _compare_tuple_from(updates):
    return tuple(updates[k] for k in _COMPARE_OUTPUT_KEYS)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

_FONT_CSS = """
.gradio-container, .gradio-container * {
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans",
                 "Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo",
                 "Noto Sans CJK JP", "Noto Sans JP", system-ui, sans-serif;
}
"""


def build_app(store=None):
    import gradio as gr

    if store is None:
        store = ProjectStore()

    with gr.Blocks(title="scopus-tools UI", theme=gr.themes.Soft(), css=_FONT_CSS) as app:
        gr.Markdown("# scopus-tools 研究者業績ビューア")
        gr.Markdown(_env_banner())

        current_project_name = gr.State(value=None)
        current_researcher_name = gr.State(value=None)
        project_delete_confirm = gr.State(value=False)
        researcher_delete_confirm = gr.State(value=False)
        scopus_state = gr.State(value=None)
        kaken_state = gr.State(value=None)

        with gr.Row():
            # =========================================================
            # Left sidebar
            # =========================================================
            with gr.Column(scale=1, min_width=300):
                gr.Markdown("## 📁 プロジェクト")
                project_dropdown = gr.Dropdown(
                    label="既存プロジェクト",
                    choices=_project_choices(store),
                    value=None,
                    interactive=True,
                    allow_custom_value=False,
                )
                with gr.Row():
                    new_project_input = gr.Textbox(
                        label="新規プロジェクト名",
                        placeholder="例: 広島大 CS 2026",
                        scale=3,
                    )
                    new_project_btn = gr.Button("➕", scale=0)
                with gr.Row():
                    rename_project_input = gr.Textbox(
                        label="名前変更",
                        placeholder="新しい名前",
                        scale=3,
                    )
                    rename_project_btn = gr.Button("✏️", scale=0)
                delete_project_btn = gr.Button("🗑️ プロジェクトを削除")
                project_status = gr.Markdown()
                gr.Markdown("---")

                gr.Markdown("## 👤 研究者")
                researcher_radio = gr.Radio(
                    label="プロジェクト内の研究者",
                    choices=[],
                    value=None,
                    interactive=True,
                )
                with gr.Row():
                    new_researcher_input = gr.Textbox(
                        label="新規研究者名",
                        placeholder="例: Hiroyuki Okamura",
                        scale=3,
                    )
                    new_researcher_btn = gr.Button("➕", scale=0)
                with gr.Row():
                    rename_researcher_input = gr.Textbox(
                        label="名前変更",
                        placeholder="新しい名前",
                        scale=3,
                    )
                    rename_researcher_btn = gr.Button("✏️", scale=0)
                delete_researcher_btn = gr.Button("🗑️ 研究者を削除")
                researcher_status = gr.Markdown()
                gr.Markdown(f"<small>保存先: <code>{store.dir_path}</code></small>")

            # =========================================================
            # Right main pane
            # =========================================================
            with gr.Column(scale=4):
                current_label = gr.Markdown(
                    "**プロジェクト未選択** — 左でプロジェクトと研究者を選択または作成してください"
                )

                with gr.Tab("1. Scopus"):
                    with gr.Row():
                        s_name = gr.Textbox(label="研究者名", placeholder="例: Hiroyuki Okamura", scale=4)
                        s_search_btn = gr.Button("Scopus 候補検索", variant="primary", scale=1)
                    s_search_status = gr.Markdown()
                    s_select = gr.CheckboxGroup(
                        label="Scopus 候補(該当者をチェック。同一人物が複数 ID に分裂している場合は全て ON)",
                        choices=[], value=[], visible=False,
                    )
                    _default_ys, _default_ye = default_eval_year_range()
                    with gr.Row():
                        s_year_start = gr.Number(label="評価開始年", value=_default_ys, precision=0)
                        s_year_end = gr.Number(label="評価終了年", value=_default_ye, precision=0)
                        s_run_btn = gr.Button("Scopus 集計を実行", variant="primary")
                    s_run_status = gr.Markdown()
                    s_result = gr.Markdown()
                    with gr.Row():
                        s_copy_btn = gr.Button("📋 コピー", scale=0)
                        s_download_btn = gr.Button("📤 エクスポート", scale=0)

                with gr.Tab("2. KAKEN"):
                    with gr.Row():
                        k_name = gr.Textbox(label="研究者名", placeholder="例: 岡村 寛之", scale=4)
                        k_search_btn = gr.Button("KAKEN 候補検索", variant="primary", scale=1)
                    k_search_status = gr.Markdown()
                    k_select = gr.CheckboxGroup(
                        label="KAKEN 候補(該当者をチェック。0 件の場合は下で直接 ID を指定)",
                        choices=[], value=[], visible=False,
                    )
                    k_manual = gr.Textbox(
                        label="KAKEN 研究者番号を直接指定(任意・8 桁、カンマ/空白区切りで複数可)",
                        placeholder="例: 10311812, 80401243",
                    )
                    k_run_btn = gr.Button("KAKEN 集計を実行", variant="primary")
                    k_run_status = gr.Markdown()
                    k_result = gr.Markdown()
                    with gr.Row():
                        k_copy_btn = gr.Button("📋 コピー", scale=0)
                        k_download_btn = gr.Button("📤 エクスポート", scale=0)

                with gr.Tab("3. AI 評価"):
                    ai_status_display = gr.Markdown(_ai_status_md(None, None))
                    ai_extra = gr.Textbox(
                        label="評価の観点・追加指示(任意)",
                        placeholder=(
                            "例: 教育能力よりも研究力を重視 / "
                            "国際共著率にも触れて / "
                            "若手研究者として評価(博士取得後 5 年)"
                        ),
                        lines=2,
                    )
                    with gr.Row():
                        ai_model = gr.Dropdown(
                            label="AI モデル",
                            choices=llm.SUPPORTED_MODELS,
                            value=llm.DEFAULT_MODEL,
                            scale=2,
                        )
                        ai_lang = gr.Dropdown(
                            label="出力言語",
                            choices=["ja", "en"],
                            value="ja",
                            scale=1,
                        )
                        ai_run_btn = gr.Button("AI 評価を実行", variant="primary", scale=1)
                    ai_run_status = gr.Markdown()
                    ai_result = gr.Markdown()
                    with gr.Row():
                        ai_copy_btn = gr.Button("📋 コピー", scale=0)
                        ai_download_btn = gr.Button("📤 エクスポート", scale=0)

                with gr.Tab("4. 比較"):
                    gr.Markdown(
                        "プロジェクト内の研究者を選んで横並び比較します。"
                        "**人事選考用**: 各人の推定分野を踏まえた AI 比較評価も生成可能。"
                    )
                    compare_select = gr.CheckboxGroup(
                        label="比較対象(プロジェクト内の研究者から選択 / 最大 10 名)",
                        choices=[], value=[], interactive=True,
                    )
                    compare_extra = gr.Textbox(
                        label="AI 評価の観点・追加指示(任意)",
                        placeholder=(
                            "例: ランキングは不要、強みと懸念だけ書いて / "
                            "テニュアトラック想定で 5 年先のポテンシャルも見て / "
                            "代表者経験を最重視"
                        ),
                        lines=2,
                    )
                    with gr.Row():
                        compare_build_btn = gr.Button("📊 比較表を生成", variant="primary")
                        compare_model = gr.Dropdown(
                            label="AI モデル",
                            choices=llm.SUPPORTED_MODELS,
                            value=llm.DEFAULT_MODEL,
                            scale=2,
                        )
                        compare_lang = gr.Dropdown(
                            label="AI 出力言語",
                            choices=["ja", "en"], value="ja", scale=1,
                        )
                        compare_ai_btn = gr.Button("🤖 AI 比較評価を実行", variant="primary")
                    compare_table_status = gr.Markdown()
                    compare_table = gr.Dataframe(
                        headers=_COMPARE_COLUMNS,
                        datatype=["str"] * len(_COMPARE_COLUMNS),
                        value=[],
                        interactive=False,
                        wrap=True,
                        col_count=(len(_COMPARE_COLUMNS), "fixed"),
                    )
                    compare_table_md_state = gr.State(value="")
                    with gr.Row():
                        compare_table_copy_btn = gr.Button("📋 表をコピー", scale=0)
                        compare_table_export_btn = gr.Button("📤 表をエクスポート", scale=0)
                    gr.Markdown("---")
                    gr.Markdown("### AI 比較評価")
                    compare_ai_status = gr.Markdown()
                    compare_ai_result = gr.Markdown()
                    with gr.Row():
                        compare_ai_copy_btn = gr.Button("📋 コピー", scale=0)
                        compare_ai_export_btn = gr.Button("📤 エクスポート", scale=0)

        right_outputs = [
            s_name, s_search_status, s_select,
            s_year_start, s_year_end, s_run_status, s_result,
            k_name, k_search_status, k_select, k_manual,
            k_run_status, k_result,
            ai_status_display, ai_lang, ai_model, ai_extra,
            ai_run_status, ai_result,
            scopus_state, kaken_state,
        ]
        compare_outputs = [
            compare_select, compare_extra, compare_model,
            compare_table, compare_table_md_state,
            compare_table_status, compare_ai_result, compare_ai_status,
        ]

        # ====================================================================
        # Project handlers
        # ====================================================================

        def _label_for(pname, rname):
            if pname and rname:
                return f"### 📂 {pname}  /  👤 {rname}"
            if pname:
                return f"### 📂 {pname}(研究者未選択 — 左で選択または作成)"
            return "**プロジェクト未選択** — 左で選択または作成してください"

        def _on_project_change(name):
            """プロジェクト切替: 研究者リストを更新し、右ペインをリセット、比較タブを復元。"""
            if not name:
                return (
                    gr.update(choices=[], value=None),
                    None,  # researcher name reset
                    _label_for(None, None),
                    *_right_tuple_from(_empty_right_pane_updates()),
                    *_compare_tuple_from(_compare_pane_empty()),
                )
            project = store.load(name) or empty_project(name)
            r_choices = _researcher_choices(project)
            return (
                gr.update(choices=r_choices, value=None),
                None,
                _label_for(name, None),
                *_right_tuple_from(_empty_right_pane_updates()),
                *_compare_tuple_from(_compare_pane_from_project(project)),
            )

        def _on_new_project(name):
            name = (name or "").strip()
            if not name:
                return (
                    gr.update(),
                    gr.update(),
                    None, None,
                    "⚠️ プロジェクト名を入力してください",
                    _label_for(None, None),
                    *_right_tuple_from(_empty_right_pane_updates()),
                    *_compare_tuple_from(_compare_pane_empty()),
                )
            unique = store.make_unique_name(name)
            store.save(unique, empty_project(unique))
            return (
                gr.update(choices=_project_choices(store), value=unique),
                gr.update(choices=[], value=None),
                unique, None,
                f"✅ プロジェクト '{unique}' を作成しました",
                _label_for(unique, None),
                *_right_tuple_from(_empty_right_pane_updates()),
                *_compare_tuple_from(_compare_pane_empty()),
            )

        def _on_rename_project(new_name, current):
            new_name = (new_name or "").strip()
            if not current:
                return gr.update(), current, "⚠️ リネーム対象のプロジェクトを選んでください", gr.update()
            if not new_name:
                return gr.update(), current, "⚠️ 新しい名前を入力してください", gr.update()
            if new_name == current:
                return gr.update(), current, "⚠️ 同じ名前です", gr.update()
            try:
                store.rename(current, new_name)
            except FileExistsError:
                return gr.update(), current, f"❌ '{new_name}' は既に存在します", gr.update()
            return (
                gr.update(choices=_project_choices(store), value=new_name),
                new_name,
                f"✅ '{current}' → '{new_name}'",
                gr.update(value=_label_for(new_name, None)),
            )

        def _on_delete_project(current, confirm):
            if not current:
                return (
                    gr.update(), gr.update(),
                    None, None, False,
                    "⚠️ 削除対象のプロジェクトを選んでください",
                    _label_for(None, None),
                    *_right_tuple_from(_empty_right_pane_updates()),
                    *_compare_tuple_from(_compare_pane_empty()),
                )
            if not confirm:
                no_op_right = _right_tuple_from({
                    **_empty_right_pane_updates(),
                    **{k: gr.update() for k in _RIGHT_OUTPUT_KEYS if k not in ("scopus_state", "kaken_state")},
                    "scopus_state": gr.update(),
                    "kaken_state": gr.update(),
                })
                no_op_compare = tuple(gr.update() for _ in _COMPARE_OUTPUT_KEYS)
                return (
                    gr.update(), gr.update(),
                    current, gr.update(), True,
                    f"⚠️ '{current}' を本当に削除しますか? もう一度押すと確定します",
                    gr.update(),
                    *no_op_right,
                    *no_op_compare,
                )
            store.delete(current)
            return (
                gr.update(choices=_project_choices(store), value=None),
                gr.update(choices=[], value=None),
                None, None, False,
                f"🗑️ '{current}' を削除しました",
                _label_for(None, None),
                *_right_tuple_from(_empty_right_pane_updates()),
                *_compare_tuple_from(_compare_pane_empty()),
            )

        # ====================================================================
        # Researcher handlers
        # ====================================================================

        def _on_researcher_change(rname, pname):
            """研究者切替: その研究者のデータを右ペインに復元。"""
            if not pname or not rname:
                return (
                    rname,
                    _label_for(pname, None),
                    *_right_tuple_from(_empty_right_pane_updates()),
                )
            project = store.load(pname) or empty_project(pname)
            researcher = find_researcher(project, rname)
            return (
                rname,
                _label_for(pname, rname),
                *_right_tuple_from(_researcher_to_updates(researcher)),
            )

        def _on_new_researcher(name, pname):
            name = (name or "").strip()
            if not pname:
                return (
                    gr.update(), gr.update(), None,
                    "⚠️ 先にプロジェクトを選択または作成してください",
                    _label_for(None, None),
                    *_right_tuple_from(_empty_right_pane_updates()),
                )
            if not name:
                return (
                    gr.update(), gr.update(), None,
                    "⚠️ 研究者名を入力してください",
                    gr.update(),
                    *_right_tuple_from(_empty_right_pane_updates()),
                )
            project = store.load(pname) or empty_project(pname)
            unique = make_unique_researcher_name(project, name)
            add_researcher(project, unique)
            store.save(pname, project)
            return (
                gr.update(choices=_project_choices(store), value=pname),
                gr.update(choices=_researcher_choices(project), value=unique),
                unique,
                f"✅ 研究者 '{unique}' を作成しました",
                _label_for(pname, unique),
                *_right_tuple_from(_empty_right_pane_updates(researcher_name=unique)),
            )

        def _on_rename_researcher(new_name, pname, current):
            new_name = (new_name or "").strip()
            if not pname or not current:
                return gr.update(), current, "⚠️ プロジェクトと研究者を選択してください", gr.update()
            if not new_name:
                return gr.update(), current, "⚠️ 新しい名前を入力してください", gr.update()
            if new_name == current:
                return gr.update(), current, "⚠️ 同じ名前です", gr.update()
            project = store.load(pname) or empty_project(pname)
            ok = rename_researcher(project, current, new_name)
            if not ok:
                return gr.update(), current, f"❌ '{new_name}' は既に存在します", gr.update()
            store.save(pname, project)
            return (
                gr.update(choices=_researcher_choices(project), value=new_name),
                new_name,
                f"✅ '{current}' → '{new_name}'",
                gr.update(value=_label_for(pname, new_name)),
            )

        def _on_delete_researcher(pname, current, confirm):
            if not pname or not current:
                return (
                    gr.update(), current, False,
                    "⚠️ 削除対象を選択してください",
                    gr.update(),
                    *_right_tuple_from(_empty_right_pane_updates()),
                )
            if not confirm:
                return (
                    gr.update(), current, True,
                    f"⚠️ 研究者 '{current}' を削除しますか? もう一度押すと確定します",
                    gr.update(),
                    *_right_tuple_from({k: gr.update() for k in _RIGHT_OUTPUT_KEYS
                                        if k not in ("scopus_state", "kaken_state")},
                                       ),
                )
            project = store.load(pname) or empty_project(pname)
            remove_researcher(project, current)
            store.save(pname, project)
            return (
                gr.update(choices=_researcher_choices(project), value=None),
                None, False,
                f"🗑️ 研究者 '{current}' を削除しました",
                _label_for(pname, None),
                *_right_tuple_from(_empty_right_pane_updates()),
            )

        # ====================================================================
        # Wiring: Project
        # ====================================================================

        project_dropdown.change(
            _on_project_change,
            inputs=[project_dropdown],
            outputs=[researcher_radio, current_researcher_name, current_label,
                     *right_outputs, *compare_outputs],
        ).then(
            lambda n: n, inputs=[project_dropdown], outputs=[current_project_name],
        )

        new_project_btn.click(
            _on_new_project,
            inputs=[new_project_input],
            outputs=[project_dropdown, researcher_radio,
                     current_project_name, current_researcher_name,
                     project_status, current_label, *right_outputs, *compare_outputs],
        ).then(lambda: "", inputs=None, outputs=[new_project_input])

        rename_project_btn.click(
            _on_rename_project,
            inputs=[rename_project_input, current_project_name],
            outputs=[project_dropdown, current_project_name, project_status, current_label],
        ).then(lambda: "", inputs=None, outputs=[rename_project_input])

        delete_project_btn.click(
            _on_delete_project,
            inputs=[current_project_name, project_delete_confirm],
            outputs=[project_dropdown, researcher_radio,
                     current_project_name, current_researcher_name, project_delete_confirm,
                     project_status, current_label, *right_outputs, *compare_outputs],
        )

        # ====================================================================
        # Wiring: Researcher
        # ====================================================================

        researcher_radio.change(
            _on_researcher_change,
            inputs=[researcher_radio, current_project_name],
            outputs=[current_researcher_name, current_label, *right_outputs],
        )

        def _refresh_compare_select(pname):
            return _compare_select_refresh(store.load(pname) if pname else None)

        new_researcher_btn.click(
            _on_new_researcher,
            inputs=[new_researcher_input, current_project_name],
            outputs=[project_dropdown, researcher_radio, current_researcher_name,
                     researcher_status, current_label, *right_outputs],
        ).then(lambda: "", inputs=None, outputs=[new_researcher_input]) \
         .then(_refresh_compare_select, inputs=[current_project_name], outputs=[compare_select])

        rename_researcher_btn.click(
            _on_rename_researcher,
            inputs=[rename_researcher_input, current_project_name, current_researcher_name],
            outputs=[researcher_radio, current_researcher_name, researcher_status, current_label],
        ).then(lambda: "", inputs=None, outputs=[rename_researcher_input]) \
         .then(_refresh_compare_select, inputs=[current_project_name], outputs=[compare_select])

        delete_researcher_btn.click(
            _on_delete_researcher,
            inputs=[current_project_name, current_researcher_name, researcher_delete_confirm],
            outputs=[researcher_radio, current_researcher_name, researcher_delete_confirm,
                     researcher_status, current_label, *right_outputs],
        ).then(_refresh_compare_select, inputs=[current_project_name], outputs=[compare_select])

        # ====================================================================
        # Wiring: Tab handlers
        # ====================================================================

        def _refresh_researcher_radio(pname):
            if not pname:
                return gr.update()
            project = store.load(pname) or empty_project(pname)
            return gr.update(choices=_researcher_choices(project))

        def _do_scopus_search(pname, rname, name, progress=gr.Progress()):
            if not pname or not rname:
                return gr.update(), "⚠️ 先にプロジェクトと研究者を選択してください", gr.update()
            sel_update, status = _scopus_search(store, pname, rname, name, progress)
            return sel_update, status, _refresh_researcher_radio(pname)

        def _do_scopus_run(pname, rname, ids, ys, ye, kstate, progress=gr.Progress()):
            if not pname or not rname:
                return None, "", "⚠️ 先にプロジェクトと研究者を選択してください", _ai_status_md(None, None), gr.update()
            state, biblio, status, _ = _scopus_run(store, pname, rname, ids, ys, ye, progress)
            return state, biblio, status, _ai_status_md(state, kstate), _refresh_researcher_radio(pname)

        def _do_kaken_search(pname, rname, name, progress=gr.Progress()):
            if not pname or not rname:
                return gr.update(), "⚠️ 先にプロジェクトと研究者を選択してください", gr.update()
            sel_update, status = _kaken_search(store, pname, rname, name, progress)
            return sel_update, status, _refresh_researcher_radio(pname)

        def _do_kaken_run(pname, rname, ids, manual, sstate, progress=gr.Progress()):
            if not pname or not rname:
                return None, "", "⚠️ 先にプロジェクトと研究者を選択してください", _ai_status_md(None, None), gr.update()
            state, kmd, status, _ = _kaken_run(store, pname, rname, ids, manual, progress)
            return state, kmd, status, _ai_status_md(sstate, state), _refresh_researcher_radio(pname)

        def _do_ai_run(pname, rname, sstate, kstate, lang, model, extra, progress=gr.Progress()):
            if not pname or not rname:
                yield "", "⚠️ 先にプロジェクトと研究者を選択してください", gr.update()
                return
            ai_md, status = "", ""
            for ai_md, status in _ai_run(store, pname, rname, sstate, kstate, lang,
                                         extra, model, progress):
                yield ai_md, status, gr.update()
            yield ai_md, status, _refresh_researcher_radio(pname)

        s_search_btn.click(_do_scopus_search,
                           inputs=[current_project_name, current_researcher_name, s_name],
                           outputs=[s_select, s_search_status, researcher_radio])
        s_name.submit(_do_scopus_search,
                      inputs=[current_project_name, current_researcher_name, s_name],
                      outputs=[s_select, s_search_status, researcher_radio])
        s_run_btn.click(_do_scopus_run,
                        inputs=[current_project_name, current_researcher_name,
                                s_select, s_year_start, s_year_end, kaken_state],
                        outputs=[scopus_state, s_result, s_run_status, ai_status_display, researcher_radio])

        k_search_btn.click(_do_kaken_search,
                           inputs=[current_project_name, current_researcher_name, k_name],
                           outputs=[k_select, k_search_status, researcher_radio])
        k_name.submit(_do_kaken_search,
                      inputs=[current_project_name, current_researcher_name, k_name],
                      outputs=[k_select, k_search_status, researcher_radio])
        k_run_btn.click(_do_kaken_run,
                        inputs=[current_project_name, current_researcher_name,
                                k_select, k_manual, scopus_state],
                        outputs=[kaken_state, k_result, k_run_status, ai_status_display, researcher_radio])

        ai_run_btn.click(_do_ai_run,
                         inputs=[current_project_name, current_researcher_name,
                                 scopus_state, kaken_state, ai_lang, ai_model, ai_extra],
                         outputs=[ai_result, ai_run_status, researcher_radio])

        # ---- Comparison tab handlers
        def _do_compare_build(pname, selected):
            if not pname:
                return [], "", "⚠️ プロジェクトを選択してください"
            rows, md, status = _build_comparison_table(store, pname, selected)
            return rows, md, status

        def _do_compare_run(pname, selected, lang, extra, model, progress=gr.Progress()):
            ai_md, status = "", ""
            for ai_md, status in _compare_run(store, pname, selected, lang, extra, model, progress):
                yield ai_md, status

        compare_build_btn.click(
            _do_compare_build,
            inputs=[current_project_name, compare_select],
            outputs=[compare_table, compare_table_md_state, compare_table_status],
        )
        compare_ai_btn.click(
            _do_compare_run,
            inputs=[current_project_name, compare_select, compare_lang, compare_extra, compare_model],
            outputs=[compare_ai_result, compare_ai_status],
        )

        # ---- Copy / Export wiring(クライアント JS のみ)
        s_copy_btn.click(fn=None, inputs=[s_result], outputs=[], js=_COPY_JS)
        s_download_btn.click(fn=None, inputs=[s_result], outputs=[],
                             js=_download_js("scopus_summary"))
        k_copy_btn.click(fn=None, inputs=[k_result], outputs=[], js=_COPY_JS)
        k_download_btn.click(fn=None, inputs=[k_result], outputs=[],
                             js=_download_js("kaken_summary"))
        ai_copy_btn.click(fn=None, inputs=[ai_result], outputs=[], js=_COPY_JS)
        ai_download_btn.click(fn=None, inputs=[ai_result], outputs=[],
                              js=_download_js("ai_evaluation"))
        # 比較表は Dataframe なので Markdown 文字列を保存している State から copy/export
        compare_table_copy_btn.click(fn=None, inputs=[compare_table_md_state],
                                     outputs=[], js=_COPY_JS)
        compare_table_export_btn.click(fn=None, inputs=[compare_table_md_state], outputs=[],
                                       js=_download_js("comparison_table"))
        compare_ai_copy_btn.click(fn=None, inputs=[compare_ai_result], outputs=[], js=_COPY_JS)
        compare_ai_export_btn.click(fn=None, inputs=[compare_ai_result], outputs=[],
                                    js=_download_js("comparison_ai"))

    return app


def launch(host="127.0.0.1", port=7860, share=False, projects_dir=None):
    from dotenv import load_dotenv

    load_dotenv()
    store = ProjectStore(projects_dir) if projects_dir else ProjectStore()
    logger.info("Projects directory: %s", store.dir_path)
    app = build_app(store)
    app.launch(server_name=host, server_port=port, share=share, inbrowser=True)
