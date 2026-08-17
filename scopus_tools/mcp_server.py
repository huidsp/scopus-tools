"""MCP (Model Context Protocol) サーバ。

`cli` と並ぶもう 1 つのフロントエンドで、Scopus / KAKEN / WoS 収録区分の
**データ取得**と、結果を蓄積する**プロジェクト永続化**をツールとして公開する。

このパッケージは API 経由で LLM を呼ばない。業績評価・専門性推定・研究者比較は
すべて MCP ホスト側のモデルが、ここのツールを対話的に呼んで自分で行う
(入れ子の LLM 呼び出しを避け、モデルが必要に応じて追加取得できるようにするため)。

起動: `scopus-tools mcp [--projects-dir DIR] [--scie-list CSV ...] [--scie-dir DIR]`
(stdio トランスポート)

stdio を汚さないこと: ログと進捗はすべて stderr に出す(`utils.progress` は
元から stderr、ロギングは `run()` 側で stderr ハンドラに固定する)。
"""

import contextlib
import functools
import logging
import os
import sys

import requests

from scopus_tools import (api, asof, core, httpcache, kaken, linking, projects,
                          scie, wos)

logger = logging.getLogger(__name__)

# 起動時に読み込む WoS インデックス集合 {ラベル: ISSN集合}
_INDEX_SETS = {}

# プロジェクト JSON の保存先(--projects-dir。None なら projects の既定)
_PROJECTS_DIR = None

# HTTP コンテキスト(キャッシュ DB + Session)と鮮度しきい値
_HTTP_CONTEXT = None
_STALE_POLICY = None

# クライアント類は初回ツール呼び出し時に遅延生成する(鍵が無くても起動はできる)
_scopus_client = None
_kaken_client = None
_wos_client = None
_project_store = None

# 1 回のツール応答で返す論文数の既定上限(トークン爆発の防止)
DEFAULT_PAPER_LIMIT = 200


def _error(message, **extra):
    """ツールのエラー応答。例外ではなく dict を返し、モデルが読めるようにする。"""
    return {"error": message, **extra}


def _network_guard(fn):
    """取得系ツールが投げる通信系例外を `{"error": ...}` に変換する。

    鍵の不足はもともと dict を返していたのに、**クォータ枯渇・オフライン・
    タイムアウトは素通しで例外になっていた**。実運用で先に起きるのは後者で
    (Author Search は週 5,000 件)、しかもそのときこそモデルに理由を伝えたい。
    例外のままだとホスト側にはプロトコルエラーとしか見えず、モデルは原因が
    分からないまま別の引数で叩き直す。

    `retriable` を付けるのは、モデルに「今すぐ再試行して意味があるか」を
    判断させるため。クォータ枯渇はリセットまで何度呼んでも無駄。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except httpcache.QuotaExceeded as e:
            return _error(
                f"{e.api}: API quota exhausted"
                + (f" until {e.reset_at_text}" if getattr(e, "reset_at_text", None) else "")
                + ". Retrying will not help until it resets. Previously fetched data is "
                  "still available from the cache.",
                retriable=False, quota_exhausted=True, api=e.api)
        except httpcache.OfflineError as e:
            return _error(f"Offline mode: {e}. This response is not in the cache.",
                          retriable=False, offline=True)
        except httpcache.RateLimited as e:
            return _error(f"Rate limited: {e}. Wait a few seconds and try again.",
                          retriable=True)
        except (requests.Timeout, requests.ConnectionError) as e:
            return _error(f"Network error contacting the API ({type(e).__name__}: {e}). "
                          f"The service may be slow or unreachable; try again.",
                          retriable=True)
    return wrapper


def _get_context():
    """HTTP コンテキスト。run() を通さずツールを直接呼ぶテストでも動くよう遅延生成する。"""
    global _HTTP_CONTEXT
    if _HTTP_CONTEXT is None:
        _HTTP_CONTEXT = httpcache.build_context()
    return _HTTP_CONTEXT


def _get_policy():
    global _STALE_POLICY
    if _STALE_POLICY is None:
        _STALE_POLICY = asof.StalePolicy()
    return _STALE_POLICY


def _get_scopus():
    global _scopus_client
    if not os.getenv("SCOPUS_API_KEY"):
        raise RuntimeError("SCOPUS_API_KEY is not set (put it in .env or the environment)")
    if _scopus_client is None:
        _scopus_client = api.ScopusClient(context=_get_context())
    return _scopus_client


def _get_kaken():
    global _kaken_client
    if not os.getenv("KAKEN_APP_ID"):
        raise RuntimeError("KAKEN_APP_ID is not set (put it in .env or the environment)")
    if _kaken_client is None:
        _kaken_client = kaken.KakenClient(context=_get_context())
    return _kaken_client


def _get_wos():
    global _wos_client
    if not os.getenv("WOS_API_KEY"):
        raise RuntimeError("WOS_API_KEY is not set (put it in .env or the environment)")
    if _wos_client is None:
        _wos_client = wos.WosClient(context=_get_context())
    return _wos_client


@contextlib.contextmanager
def _fetching(client, refresh=False):
    """取得メタデータを集めつつ、この呼び出しだけ refresh を効かせる。

    refresh はレイヤの状態ではなく**呼び出しごとの上書き**にする
    (状態にすると次の呼び出しに漏れる)。
    """
    layer = getattr(client, "_http", None)
    if layer is None:                                  # pragma: no cover
        yield []
        return
    previous = layer.refresh
    if refresh:
        layer.refresh = True
    try:
        with layer.collect() as records:
            yield records
    finally:
        layer.refresh = previous


def _as_of(records, default_api="generic"):
    """収集した取得メタデータから as_of / as_of_note を作る。

    操作全体の as-of は、それを構成したリクエストの**最も古い取得日時**。
    """
    policy = _get_policy()
    # records は真値だが空、ということがある(クライアントをまるごとモックした
    # テストなど)。必ずリストに落としてから判定する。
    try:
        items = [r for r in (records or []) if isinstance(r, dict) and r.get("fetched_at")]
    except TypeError:
        items = []
    if not items:
        entry = asof.describe(None, default_api, policy, cached=False)
        return entry.to_dict(), entry.note()
    oldest = min(items, key=lambda r: r["fetched_at"])
    any_fresh = any(not r["cached"] for r in items)
    entry = asof.describe(oldest["fetched_at"], oldest["api"], policy,
                          cached=not any_fresh)
    payload = entry.to_dict()
    payload["requests"] = len(items)
    payload["from_cache"] = sum(1 for r in items if r["cached"])
    return payload, entry.note()


def _attach_completeness(payload, fetched, paginated=True):
    """取得が不完全なら、その事実をモデルに明示する。

    `truncated`(こちらが limit で切った)とは別物。`incomplete` は
    **Scopus から全部取れなかった**という意味で、業績の全体像として扱えない。

    `paginated=False` は `find_papers` のような 1 リクエストで完結する経路。
    「途中のページで止まった」という案内はそこでは事実に反するので出さない。
    """
    if fetched is None or getattr(fetched, "complete", True):
        return payload
    payload["incomplete"] = True
    payload["incomplete_reason"] = fetched.reason

    note = [f"WARNING: the fetch did not complete ({fetched.reason})."]
    if fetched.expected_total is None:
        # 総件数すら返ってきていない(認証エラー等)。「about None」と出さない。
        note.append(f"Got {fetched.actual_total} records; Scopus did not report a total, "
                    f"so how much is missing is unknown.")
    else:
        note.append(f"Got {fetched.actual_total} of about {fetched.expected_total} records.")
    note.append("Do not present these counts as a full publication record.")
    if paginated:
        note.append("Pagination stopped at the failing page, so call this tool again to "
                    "fetch the rest — the pages already retrieved come from cache and "
                    "cost no quota. Do NOT pass refresh=true (it would discard them).")
    else:
        note.append("This lookup is a single request, so retrying it is cheap; "
                    "if it keeps failing, the API key or network is the problem, "
                    "not the query.")
    payload["incomplete_note"] = " ".join(note)
    return payload


def _with_as_of(payload, records, default_api="generic"):
    meta, note = _as_of(records, default_api)
    payload["as_of"] = meta
    payload["as_of_note"] = note
    return payload


def _get_store():
    global _project_store
    if _project_store is None:
        _project_store = projects.ProjectStore(_PROJECTS_DIR)
    return _project_store


def _split_ids(author_ids):
    """'123,456' / ['123','456'] のどちらでも受け取り、リストに正規化する。"""
    if isinstance(author_ids, str):
        items = author_ids.split(",")
    else:
        items = list(author_ids or [])
    return [str(i).strip() for i in items if str(i).strip()]


def _resolve_years(year_range):
    """MCP 引数の year_range 文字列を (start, end) に解決する。"""
    return core.parse_year_range(year_range)


# ---------------------------------------------------------------------------
# ツール実体(MCP 層を通さず直接テストできるよう、素の関数として定義する)
# ---------------------------------------------------------------------------

@_network_guard
def search_author(first_name, last_name, refresh=False):
    """Scopus 著者候補を検索する(1 リクエスト)。

    姓と名を**分けて**渡すこと。例: "Hiroyuki Okamura" なら
    first_name="Hiroyuki", last_name="Okamura"。

    Author Search は週 5,000 件と最も厳しいクォータなので、順序を当てるために
    2 回投げることはしない。0 件だった場合、氏名の表記順を取り違えている可能性が
    あるので、first_name と last_name を入れ替えてもう一度呼ぶこと。
    返る候補の `id` を他のツールの author_ids に使う。
    refresh=True でキャッシュを無視して取り直す。
    """
    try:
        client = _get_scopus()
    except RuntimeError as e:
        return _error(str(e))
    with _fetching(client, refresh) as records:
        results = client.search_author(first_name, last_name)
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "count": len(results),
        "candidates": results,
    }
    if not results:
        payload["hint"] = ("No match. If the name order might be reversed, call again with "
                           f"first_name={last_name!r}, last_name={first_name!r}.")
    return _with_as_of(payload, records, "scopus_author_search")


@_network_guard
def find_papers(title=None, doi=None, author_last_name=None, limit=10,
                include_abstract=False, refresh=False):
    """タイトルや DOI から論文を引き、**著者を Scopus Author ID 付きで**返す。

    主な用途は **分裂した Author ID の特定**。ある研究者の論文が複数の Author ID に
    分かれているとき(`author_summary` の論文数が本人の実績より明らかに少ないとき)、
    欠けている論文を 1 件このツールで引き、`authors_detail` の中からその研究者に
    あたる `authid` を読み取る。見つけた ID は
    `author_summary(author_ids="既知のID,新しいID")` のようにカンマ区切りで渡せば
    重複を除いて合算される。

    title / doi / author_last_name は任意で、指定したものが AND で結合される。
    DOI が分かっていれば最も確実。**タイトルの照合は緩く、先頭数語や 1 語の綴り違いでも
    当たる**ので、複数返ったら誌名・年・著者で正しい論文か確認すること。

    誌名・ISSN・巻号・ページ・DOI・オープンアクセス、著者の ORCID、所属機関
    (名称・都市・国)、著者キーワードも返す。抄録は大きいので
    include_abstract=True のときだけ含める。
    """
    try:
        client = _get_scopus()
    except RuntimeError as e:
        return _error(str(e))
    try:
        with _fetching(client, refresh) as records:
            fetched = client.find_papers(
                title=title, doi=doi, author_last_name=author_last_name,
                limit=limit, include_abstract=include_abstract)
    except ValueError as e:
        return _error(str(e))

    payload = {
        "query": {"title": title, "doi": doi, "author_last_name": author_last_name},
        "total_count": fetched.expected_total,
        "returned_count": len(fetched.papers),
        "papers": fetched.papers,
    }
    # 「見つからなかった」と言えるのは検索が成功したときだけ。取得自体が失敗
    # (401 等)しているのに「語数を減らして再検索を」と促すと、モデルは原因に
    # たどり着けないままタイトルを変えて延々と試し続ける。
    if not fetched.papers and fetched.complete:
        payload["hint"] = ("No match. Scopus title matching is loose, so try fewer words, "
                           "or use the DOI if you have it.")
    _attach_completeness(payload, fetched, paginated=False)
    return _with_as_of(payload, records, "scopus_search")


@_network_guard
def author_profile(author_id, refresh=False):
    """Scopus 著者 ID からプロフィール(姓名)を取得する。"""
    try:
        client = _get_scopus()
    except RuntimeError as e:
        return _error(str(e))
    with _fetching(client, refresh) as records:
        first, last = client.get_author_profile(author_id)
    return _with_as_of(
        {"scopus_id": str(author_id), "first_name": first, "last_name": last},
        records, "scopus_author_retrieval")


@_network_guard
def author_summary(author_ids, year_range=None, refresh=False):
    """著者の書誌指標(H/G-index、被引用、筆頭著者数、WoS 収録数)をまとめる。

    応答の `as_of` / `as_of_note` に「いつ時点のデータか」が入る。被引用数は
    時間とともに増えるので、複数の研究者を比較するときは取得日をそろえること
    (古い方を refresh=True で取り直す)。
    """
    try:
        client = _get_scopus()
        years = _resolve_years(year_range)
    except (RuntimeError, ValueError) as e:
        return _error(str(e))
    ids = _split_ids(author_ids)
    if not ids:
        return _error("author_ids is empty")
    with _fetching(client, refresh) as records:
        fetched = client.search_papers_detailed(ids)
        first, last = client.get_author_profile(ids[0])
    papers = fetched.papers
    if _INDEX_SETS:
        scie.annotate_papers_indexes(papers, _INDEX_SETS)
    report = core.summarize_papers(papers, year_range=years)
    payload = {
        "scopus_ids": ids,
        "author": {"first_name": first, "last_name": last},
        "year_range": list(years),
        "wos_indexes_loaded": sorted(_INDEX_SETS.keys()),
        "summary": report,
    }
    _attach_completeness(payload, fetched)
    return _with_as_of(payload, records, "scopus_search")


@_network_guard
def list_papers(author_ids, year_range=None, limit=DEFAULT_PAPER_LIMIT, scie_only=False,
                include_author_ids=False, refresh=False):
    """指定期間に出版された論文を、著者順位と WoS 収録インデックス付きで列挙する。

    応答の `as_of` / `as_of_note` に「いつ時点のデータか」が入る。
    refresh=True でキャッシュを無視して取り直す。

    include_author_ids=True にすると各論文に共著者の Scopus Author ID が付く。
    既定で付けないのは応答が大きくなるため。1 本の論文の著者 ID を知りたいだけなら
    `find_papers` の方が軽い。
    """
    try:
        client = _get_scopus()
        start_y, end_y = _resolve_years(year_range)
    except (RuntimeError, ValueError) as e:
        return _error(str(e))
    ids = _split_ids(author_ids)
    if not ids:
        return _error("author_ids is empty")
    if scie_only and not _INDEX_SETS:
        return _error(
            "scie_only requires WoS index CSVs; start the server with "
            "--scie-list or --scie-dir"
        )

    query_extra = f"PUBYEAR > {start_y - 1} AND PUBYEAR < {end_y + 1}"
    with _fetching(client, refresh) as records:
        fetched = client.search_papers_detailed(ids, query_extra=query_extra,
                                                detail=include_author_ids)
    papers = fetched.papers
    if _INDEX_SETS:
        scie.annotate_papers_indexes(papers, _INDEX_SETS)
        if scie_only:
            papers = [p for p in papers if p.get("wos_indexes")]

    papers.sort(key=lambda p: (-(p.get("year") or 0), -(p.get("citations") or 0)))
    total = len(papers)
    limit = max(1, int(limit or DEFAULT_PAPER_LIMIT))
    truncated = total > limit
    payload = {
        "scopus_ids": ids,
        "year_range": [start_y, end_y],
        "total_count": total,
        "returned_count": min(total, limit),
        "truncated": truncated,
        "papers": papers[:limit],
    }
    _attach_completeness(payload, fetched)
    return _with_as_of(payload, records, "scopus_search")


@_network_guard
def kaken_search_researcher(name, refresh=False):
    """研究者名から KAKEN(NRID)の研究者候補を検索する。"""
    try:
        client = _get_kaken()
    except RuntimeError as e:
        return _error(str(e))
    with _fetching(client, refresh) as records:
        results = client.search_researcher_by_name(name)
    return _with_as_of({"query": name, "count": len(results), "candidates": results},
                       records, "kaken_researcher")


@_network_guard
def kaken_grants(researcher_id, role=None, refresh=False):
    """研究者番号から KAKEN 課題(科研費)の一覧を取得する。"""
    try:
        client = _get_kaken()
    except RuntimeError as e:
        return _error(str(e))
    with _fetching(client, refresh) as records:
        grants = client.get_grants_by_researcher_id(researcher_id, role=role)
    return _with_as_of(
        {"researcher_id": str(researcher_id), "count": len(grants), "grants": grants},
        records, "kaken_project")


@_network_guard
def link_kaken_researcher(first_name, last_name, auto=False, refresh=False):
    """Scopus の著者氏名から KAKEN 研究者番号を名前ベースで自動照合する。

    候補が 1 件なら自動採用。複数件で auto=False のときは空リストを返すので、
    その場合は `kaken_search_researcher` を呼んで候補を自分で確認し選ぶこと。
    auto=True にすると複数件でも先頭候補を採用する。
    """
    try:
        client = _get_kaken()
    except RuntimeError as e:
        return _error(str(e))
    # MCP は非対話。stdin を読む分岐に入らないよう interactive=False を明示する。
    with _fetching(client, refresh) as records:
        ids = linking.resolve_kaken_researcher_ids(
            first_name, last_name, client, auto=auto, interactive=False)
    return _with_as_of({
        "first_name": first_name,
        "last_name": last_name,
        "researcher_ids": ids,
        "auto": bool(auto),
    }, records, "kaken_researcher")


# ---------------------------------------------------------------------------
# プロジェクト永続化(複数研究者を蓄積して比較する人事選考ワークフロー用)
# ---------------------------------------------------------------------------

def list_projects():
    """保存済みプロジェクト名の一覧を返す。"""
    store = _get_store()
    return {"projects_dir": store.dir_path, "projects": store.list()}


def _asof_report(project, section="scopus"):
    """プロジェクト内の各研究者の取得日の散らばりを見る。

    セクションの `_fetched_at` を使い、無ければ研究者の `updated_at` に落とす
    (旧いプロジェクトファイルには `_fetched_at` が無い)。
    """
    entries = []
    for r in (project.get("researchers") or []):
        sec = r.get(section) or {}
        entries.append({
            "label": r.get("name"),
            "fetched_at": sec.get("_fetched_at") or r.get("updated_at"),
        })
    return asof.spread(entries)


def _attach_asof_warning(payload, project, section="scopus"):
    report = _asof_report(project, section)
    payload["as_of_report"] = report
    if report.get("consistent") is False or report.get("known_count", 0) == 0:
        if len(project.get("researchers") or []) > 1:
            payload["as_of_warning"] = "\n".join(asof.warning_lines(report))
    return payload


def read_project(name):
    """プロジェクトを読み込んで返す(研究者一覧と比較結果を含む)。

    複数の研究者が登録されていて取得日がそろっていない場合、`as_of_warning` が付く。
    被引用数は時間とともに増えるので、取得日の違う数値をそのまま並べて比較しないこと。
    """
    store = _get_store()
    project = store.load(name)
    if project is None:
        return _error(f"project '{name}' not found")
    return _attach_asof_warning(dict(project), project)


def create_project(name):
    """空のプロジェクトを作成する。同名が既にあればエラーを返す。"""
    store = _get_store()
    if store.exists(name):
        return _error(f"project '{name}' already exists")
    project = projects.empty_project(name)
    store.save(name, project)
    return project


def delete_project(name):
    """プロジェクトを削除する。"""
    store = _get_store()
    if not store.exists(name):
        return _error(f"project '{name}' not found")
    store.delete(name)
    return {"deleted": name}


def save_researcher_section(project, researcher, section, data):
    """研究者の 1 セクションを更新して保存する。

    section は "scopus" / "kaken" / "ai" のいずれか。data は dict で、
    既存の内容に merge される(全置換ではない)。プロジェクトや研究者が
    存在しなければ作成する。
    """
    if section not in ("scopus", "kaken", "ai"):
        return _error(f"section must be one of scopus/kaken/ai (got '{section}')")
    if not isinstance(data, dict):
        return _error("data must be an object")
    store = _get_store()
    proj = store.load(project) or projects.empty_project(project)
    # 取得日を記録しておく。比較時に「取得日がそろっているか」を判定するのに使う。
    payload = dict(data)
    payload.setdefault("_fetched_at", projects._now_iso())
    projects.merge_researcher_section(proj, researcher, section, payload)
    store.save(project, proj)
    return {"project": project, "researcher": researcher, "section": section,
            "fetched_at": payload["_fetched_at"]}


def save_comparison(project, table_md=None, ai_evaluation=None, selected_names=None):
    """プロジェクト直下に複数研究者の比較結果を保存する。"""
    store = _get_store()
    proj = store.load(project)
    if proj is None:
        return _error(f"project '{project}' not found")
    report = _asof_report(proj)
    saved = projects.set_project_comparison(proj, {
        "selected_names": selected_names or [],
        "table_md": table_md or "",
        "ai_evaluation": ai_evaluation or "",
        "as_of_report": report,
    })
    store.save(project, proj)
    result = {"project": project, "comparison": saved}
    return _attach_asof_warning(result, proj)


def cache_stats():
    """レスポンスキャッシュの状態と Elsevier クォータの残量を返す。

    キャッシュは自動失効しない(取得日をそろえるため)。古いデータは
    各ツールの `refresh=true` で取り直す。
    """
    ctx = _get_context()
    if ctx.db is None:
        return {"enabled": False,
                "note": "The response cache is disabled for this server."}
    stats = ctx.db.stats()
    policy = _get_policy()
    for row in stats.get("per_api", []):
        row["stale_after_days"] = policy.days_for(row["api"])
    stats["enabled"] = True
    stats["stale_thresholds"] = policy.describe_all()
    return stats


def _wos_payload(fetched, extra, limit):
    payload = dict(extra)
    payload.update({
        "total_count": fetched.expected_total,
        "returned_count": len(fetched.papers),
        "truncated": bool(limit and (fetched.expected_total or 0) > limit),
        "papers": fetched.papers,
        "source": "wos",
    })
    _attach_completeness(payload, fetched)
    return payload


@_network_guard
def wos_find_document(doi=None, title=None, author_last_name=None, limit=10,
                      refresh=False):
    """Web of Science で論文を引き、**著者を ResearcherID 付きで**返す。

    主な用途は `wos_author_documents` に渡す著者識別子の入手。論文を 1 件引いて
    `authors_detail` の `researcher_id` を読み取り、それを渡せば同姓同名に
    汚染されない業績一覧が得られる。DOI 指定が最も確実。

    Scopus に無い論文の確認にも使える(WoS にあって Scopus に無い、あるいはその逆)。
    """
    try:
        client = _get_wos()
    except (RuntimeError, ValueError) as e:
        return _error(str(e))
    try:
        with _fetching(client, refresh) as records:
            fetched = client.find_documents(doi=doi, title=title,
                                            author_last_name=author_last_name,
                                            limit=limit)
    except ValueError as e:
        return _error(str(e))
    payload = _wos_payload(fetched, {
        "query": {"doi": doi, "title": title, "author_last_name": author_last_name},
    }, limit)
    if not fetched.papers and fetched.complete:
        payload["hint"] = "No match in Web of Science. Try the DOI, or fewer title words."
    return _with_as_of(payload, records, "wos_documents")


@_network_guard
def wos_author_documents(researcher_id=None, name=None, organization=None,
                         year_range=None, limit=DEFAULT_PAPER_LIMIT,
                         scie_only=False, refresh=False):
    """Web of Science で著者の業績を取得する(WoS の Times Cited 付き)。

    **著者の指定方法で結果の性格が大きく変わる。両方の欠点を理解して使うこと:**

    - `researcher_id`(ResearcherID または ORCID、`AI=` 検索)—— **高精度・低再現率**。
      本人の記録しか返らないが、ResearcherID が紐付いていない記録は落ちる。
      実測: ある研究者で 80 件(Scopus では 244 件)。**下限値**として扱うこと。
    - `name` + `organization`(`AU=` + `OG=`)—— **高再現率・低精度**。
      実測: 同じ研究者で 255 件だが、うち 176 件は同姓の別人(1970 年代の
      一般相対論の論文)だった。`organization` を省くとさらに悪化する(2,996 件)。

    つまり **どちらの数字もそのまま業績数として報告してはいけない**。
    確実なのは、`wos_find_document` で ResearcherID を得てから `researcher_id` で
    引き、足りない分を DOI 単位で補うこと。

    注意: Starter API は **SCIE / SSCI などの収録版を返さない**。`wos_indexes` は
    従来どおり ISSN と Master Journal List CSV の突き合わせで付与される。
    """
    try:
        client = _get_wos()
    except (RuntimeError, ValueError) as e:
        return _error(str(e))
    if scie_only and not _INDEX_SETS:
        return _error("scie_only requires index CSVs (start the server with --scie-list/--scie-dir)")
    years = _resolve_years(year_range) if year_range else None
    try:
        with _fetching(client, refresh) as records:
            fetched = client.author_documents(
                researcher_id=researcher_id, name=name, organization=organization,
                year_range=years, limit=limit)
    except ValueError as e:
        return _error(str(e))

    papers = fetched.papers
    if _INDEX_SETS:
        scie.annotate_papers_indexes(papers, _INDEX_SETS)
        if scie_only:
            papers = [p for p in papers if p.get("wos_indexes")]
            fetched.papers = papers

    payload = _wos_payload(fetched, {
        "query": {"researcher_id": researcher_id, "name": name,
                  "organization": organization, "year_range": years},
        "strategy": "researcher_id" if researcher_id else "name",
    }, limit)

    if researcher_id:
        payload["caveat"] = (
            "Matched on the author identifier, so these are certainly this person's "
            "records — but Web of Science only links records whose ResearcherID was "
            "claimed, so this is a LOWER BOUND on their output, not a total.")
    else:
        payload["caveat"] = (
            "Matched on author name" + (" and organization" if organization else "")
            + ", which admits same-name researchers — measured at 176 of 255 records "
              "belonging to a different person in one real case. Do not report this "
              "count as the researcher's output; get a ResearcherID via "
              "wos_find_document and re-run with researcher_id.")
        if not organization:
            payload["warning"] = (
                "No organization given. An author-name-only search matched 2,996 "
                "records for one researcher whose real output is a few hundred.")
    return _with_as_of(payload, records, "wos_documents")


_TOOLS = [
    # データ取得
    search_author,
    find_papers,
    author_profile,
    author_summary,
    list_papers,
    kaken_search_researcher,
    kaken_grants,
    link_kaken_researcher,
    # Web of Science(WoS の Times Cited。収録版は返らないので scie.py は併用)
    wos_find_document,
    wos_author_documents,
    # プロジェクト永続化
    list_projects,
    read_project,
    create_project,
    delete_project,
    save_researcher_section,
    save_comparison,
    # 運用
    cache_stats,
]


def _server_class():
    """SDK のサーバクラスを返す。

    MCP SDK 2.x は `mcp.server.MCPServer`、1.x は `mcp.server.fastmcp.FastMCP`。
    `add_tool` / `run(transport=...)` の形は両者で同じなので名前解決だけ吸収する。
    """
    try:
        from mcp.server import MCPServer
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP


def build_server():
    """MCP サーバを構築して返す(`mcp` パッケージが必要)。"""
    server = _server_class()("scopus-tools")
    for fn in _TOOLS:
        server.add_tool(fn)
    return server


def run(projects_dir=None, scie_list=None, scie_dir=None, cache_db=None,
        no_cache=False, stale_policy=None, timeout=None):
    """stdio トランスポートで MCP サーバを起動する。"""
    global _INDEX_SETS, _PROJECTS_DIR, _HTTP_CONTEXT, _STALE_POLICY
    _PROJECTS_DIR = projects_dir
    _STALE_POLICY = stale_policy or asof.StalePolicy()
    _HTTP_CONTEXT = httpcache.build_context(
        cache_db=cache_db, no_cache=no_cache, timeout=timeout)
    _INDEX_SETS = scie.discover_index_sets(scie_list=scie_list, scie_dir=scie_dir)
    if _INDEX_SETS:
        logger.info("WoS index lists loaded: %s",
                    ", ".join(f"{k}({len(v)})" for k, v in _INDEX_SETS.items()))
    else:
        logger.info("No WoS index CSV found (scie_dir=%s)", scie_dir)
    print(f"scopus-tools MCP server starting (tools: {len(_TOOLS)})", file=sys.stderr)
    build_server().run(transport="stdio")
