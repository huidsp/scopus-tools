"""Web of Science Starter API クライアント。Scopus / KAKEN と並ぶ第 3 のデータ源。

送信は同じ `httpcache.HttpLayer` を通る(キャッシュ・スロットル・リトライ・
`--offline` がそのまま効く)。**鍵はクエリではなくヘッダ**(`X-ApiKey`)で渡すので、
`HttpLayer(auth_headers=...)` を使う。リクエストヘッダはキャッシュキーにも DB にも
入らないため、Scopus の `apiKey` と同じく鍵がディスクに落ちる経路は無い。

**何が取れて、何が取れないか**(実測で確認)

  取れる: WoS UID、タイトル、著者(表示名・WoS 標準形・ResearcherID・**著者順**)、
          誌名・巻号・ページ・出版年月、DOI / ISSN / eISSN / ISBN / PMID、
          著者キーワード、文献タイプ、**Times Cited**(契約機関のみ)、
          引用文献・被引用文献への **URL**(中身は Expanded が必要)

  取れない: **SCIE / SSCI / AHCI / ESCI の収録版**。`Document` にも `Journal` にも
          該当フィールドが無い。`citations[].db` は "WOS" 等の**データベース**名で
          あって版ではない。したがって `scie.py` の Master Journal List CSV 突き合わせは
          **この API では置き換えられない**。ほかに抄録・所属住所・助成金・
          引用文献本体も Starter には無い(Expanded 契約が必要)。

**レート制限**(実測。レスポンスの `X-RateLimit-Remaining-Day` / `-Second` で確認できる)

  Free Trial               1 req/s /    50 req/日
  Institutional Member     5 req/s / 5,000 req/日   ← 契約機関はこれ
  Institutional Integration 5 req/s / 20,000 req/日

  1 リクエスト最大 50 件(51 を指定すると HTTP 400)。ページングの上限は緩く、
  実測で 5 万件目まで到達できた(Scopus の 5,000 件上限より深い)。

**著者の特定**

  `AI=`(著者識別子)が ORCID・ResearcherID の**どちらの書式でも引ける**ので、
  これが最も確実。分からない場合は `AU=` を `OG=`(機関)で絞る — 実測では
  `AU=(Yamada T)` の 2,996 件が `OG=` 併用で 255 件になり、Scopus 側の
  同一人物の件数(244)とほぼ一致した。`AU=` 単独の数字は使ってはいけない。
"""

import logging
import os
import re

from scopus_tools.api import FetchResult
from scopus_tools.httpcache import HttpLayer

logger = logging.getLogger(__name__)

BASE = "https://api.clarivate.com/apis/wos-starter/v1"

# 1 リクエストの最大件数。51 以上は HTTP 400。
PAGE_SIZE = 50

# 1 回の取得で辿るページ数の上限(50 x 40 = 2,000 件)。
# API 自体はもっと深くまで返せるが、1 人の業績としては十分で、
# 暴走したクエリで日次 5,000 リクエストを溶かさないための歯止め。
MAX_PAGES = 40

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def quoted(value):
    """検索式に埋め込める形にして二重引用符で囲む。

    値を丸括弧のまま裸で置くと式の括弧の対応が壊れるおそれがあるが、
    **括弧を潰してはいけない** — DOI には括弧を含むものがある
    (`10.1016/S0898-1221(99)00325-9` のような Elsevier の旧式 DOI)。
    実測では潰すと 1 件 → 0 件になった。

    二重引用符で囲めば括弧を含んだまま正しく解釈される(実測)。ついでに
    `TI=` はフレーズ検索になり、語単位の検索より絞り込みが効く(同じタイトルで
    5 件 → 1 件)。値の中の二重引用符だけは囲みを壊すので取り除く。
    """
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value).replace('"', " ")).strip()
    return f'"{cleaned}"' if cleaned else ""


def build_author_query(researcher_id=None, name=None, organization=None,
                       year_range=None):
    """著者クエリを組み立てる。

    `researcher_id`(ORCID / ResearcherID)があればそれを使うのが最も確実。
    無ければ `name` を使うが、**`organization` 併用を強く推奨**する
    (同姓同名が大量に混ざるため。モジュール冒頭の実測を参照)。
    """
    parts = []
    if researcher_id:
        parts.append(f"AI=({quoted(researcher_id)})")
    if name:
        parts.append(f"AU=({quoted(name)})")
    if not parts:
        raise ValueError("give researcher_id or name")
    if organization:
        parts.append(f"OG=({quoted(organization)})")
    if year_range:
        start_y, end_y = year_range
        parts.append(f"PY=({int(start_y)}-{int(end_y)})")
    return " AND ".join(parts)


def _publish_month(value):
    """"FEB" / "FEB-MAR" / "" を月番号に。取れなければ None。"""
    if not value:
        return None
    head = str(value).split("-")[0].strip().upper()[:3]
    return _MONTHS.get(head)


def parse_document(doc, researcher_ids=None):
    """WoS の 1 レコードを、Scopus 側 (`api.parse_entry`) と**同じ形**の dict にする。

    形を揃えるのは `core.summarize_papers` / `scie.annotate_papers_indexes` /
    `utils.print_papers_list` をそのまま通すため。出所は `source` キーで区別する。

    `researcher_ids` を渡さない場合、`is_first_author` / `author_position` は
    **None**(False や 0 ではない)。誰の論文か分からないという意味で、
    「筆頭著者ではない」とは別の主張だから — Scopus 側と同じ規約。
    """
    wanted = {str(r).strip().upper() for r in (researcher_ids or []) if r}

    names = (doc.get("names") or {}).get("authors") or []
    auth_list, authors_detail = [], []
    for pos, a in enumerate(names, start=1):
        display = a.get("displayName") or a.get("wosStandard") or ""
        auth_list.append(display)
        authors_detail.append({
            "name": display,
            "wos_standard": a.get("wosStandard") or "",
            "researcher_id": a.get("researcherId") or "",
            "orcid": a.get("orcid") or "",
            "sequence": pos,
        })

    is_first, position = None, None
    if wanted:
        ids = [(d["researcher_id"] or "").upper() for d in authors_detail]
        orcids = [(d["orcid"] or "").upper() for d in authors_detail]
        is_first = bool(ids) and (ids[0] in wanted or orcids[0] in wanted)
        for pos, (rid, orc) in enumerate(zip(ids, orcids), start=1):
            if rid in wanted or orc in wanted:
                position = pos
                break

    src = doc.get("source") or {}
    pages = src.get("pages") or {}
    ident = doc.get("identifiers") or {}

    # citations は [{db, count}] のリスト。WoS Core Collection の値を採る。
    counts = {c.get("db"): c.get("count") for c in (doc.get("citations") or [])}
    cited = counts.get("WOS")
    if cited is None and counts:
        cited = max(v for v in counts.values() if v is not None)

    types = doc.get("types") or []
    kw = (doc.get("keywords") or {}).get("authorKeywords") or []

    return {
        "title": doc.get("title"),
        "year": int(src.get("publishYear") or 0),
        "month": _publish_month(src.get("publishMonth")),
        "citations": int(cited or 0),
        "journal": src.get("sourceTitle") or "",
        "issn": ident.get("issn") or "",
        "eissn": ident.get("eissn") or "",
        "volume": src.get("volume") or "",
        "issue": src.get("issue") or "",
        "pages": pages.get("range") or "",
        "aggregation_type": "",           # Starter には該当フィールドが無い
        "type": types[0] if types else "",
        "eid": "",                        # Scopus 固有。突き合わせは doi / uid で行う
        "wos_uid": doc.get("uid") or "",
        "auth_list": auth_list,
        "authors": ", ".join(a for a in auth_list if a),
        "is_first_author": is_first,
        "author_position": position,
        "author_count": len(names),
        "doi": ident.get("doi") or "",
        "article_number": src.get("articleNumber") or "",
        "open_access": False,             # Starter は OA フラグを返さない
        "keywords": list(kw),
        "authors_detail": authors_detail,
        "links": doc.get("links") or {},
        "source": "wos",
    }


class WosClient:
    """Web of Science Starter API への唯一のネットワーク境界。"""

    def __init__(self, api_key=None, http=None, context=None):
        self.api_key = api_key or os.getenv("WOS_API_KEY")
        if not self.api_key:
            raise ValueError("WOS_API_KEY is not set.")
        # 鍵はヘッダで送る。キャッシュキー算出後に注入されるので DB には残らない。
        auth_headers = {"X-ApiKey": self.api_key}
        if http is not None:
            self._http = http
        elif context is not None:
            self._http = context.layer_for(auth_headers=auth_headers)
        else:
            self._http = HttpLayer(auth_headers=auth_headers)

    # ---- 低レベル ----------------------------------------------------

    def _get(self, path, params):
        resp = self._http.get(f"{BASE}/{path}", params=params,
                              headers={"Accept": "application/json"},
                              api="wos_documents")
        if resp.status_code != 200:
            logger.warning("WoS %s failed: status=%s body=%s", path,
                           resp.status_code, resp.text[:200].replace("\n", " "))
            return None
        try:
            return resp.json()
        except ValueError:
            logger.warning("WoS %s returned non-JSON", path)
            return None

    # ---- 検索 --------------------------------------------------------

    def search(self, query, limit=None, researcher_ids=None):
        """検索式を投げて `FetchResult` を返す。ページングは cursor ではなく page 番号。

        `complete=False` は「WoS から全部取れなかった」の意味で、`limit` で
        こちらが切ったこととは別物 — Scopus 側 `FetchResult` と同じ規約。
        """
        papers, page, expected = [], 1, None
        requests_made = 0
        while page <= MAX_PAGES:
            data = self._get("documents", {
                "q": query, "limit": PAGE_SIZE, "page": page, "db": "WOS",
            })
            requests_made += 1
            if data is None:
                return FetchResult(papers, complete=False,
                                   reason=f"HTTP error at page {page}",
                                   request_count=requests_made,
                                   expected_total=expected)
            meta = data.get("metadata") or {}
            if expected is None:
                expected = meta.get("total")
                logger.info("WoS: %s → %s 件", query, expected)
            hits = data.get("hits") or []
            if not hits:
                break
            papers.extend(parse_document(h, researcher_ids=researcher_ids)
                          for h in hits)
            if limit and len(papers) >= limit:
                break
            if expected is not None and len(papers) >= expected:
                break
            page += 1

        hit_ceiling = (page > MAX_PAGES and expected is not None
                       and len(papers) < expected)
        complete, reason = True, None
        if hit_ceiling:
            complete = False
            reason = (f"stopped at the {MAX_PAGES}-page ceiling "
                      f"({MAX_PAGES * PAGE_SIZE} records)")
        elif expected is not None and not limit and len(papers) < expected:
            complete = False
            reason = f"got {len(papers)} of {expected} records"

        return FetchResult(papers[:limit] if limit else papers,
                           complete=complete, reason=reason,
                           request_count=requests_made, expected_total=expected,
                           actual_total=len(papers))

    def author_documents(self, researcher_id=None, name=None, organization=None,
                         year_range=None, limit=None):
        """著者の業績を引く。`researcher_id`(ORCID / ResearcherID)が最も確実。

        `name` だけで `organization` を省くと同姓同名が大量に混ざるので、
        名前指定のときは機関の併用を促す(呼び出し側が判断できるよう、
        ここでは禁止ではなく警告に留める)。
        """
        query = build_author_query(researcher_id=researcher_id, name=name,
                                   organization=organization, year_range=year_range)
        if name and not researcher_id and not organization:
            logger.warning("WoS: AU= without OG= matches same-name authors; "
                           "the counts will be inflated.")
        ids = [researcher_id] if researcher_id else None
        return self.search(query, limit=limit, researcher_ids=ids)

    def find_documents(self, doi=None, title=None, author_last_name=None, limit=10):
        """DOI / タイトルから論文を引く(著者を ResearcherID 付きで返す)。

        Scopus 側 `find_papers` と同じ用途 — 1 件引いて著者識別子を読み取り、
        それを `author_documents(researcher_id=...)` に渡して業績全体を取る。
        """
        parts = []
        if doi:
            parts.append(f"DO=({quoted(doi)})")
        if title:
            safe = quoted(title)
            if not safe:
                raise ValueError("title has no searchable characters")
            parts.append(f"TI=({safe})")
        if author_last_name:
            parts.append(f"AU=({quoted(author_last_name)})")
        if not parts:
            raise ValueError("give at least one of doi / title / author_last_name")
        return self.search(" AND ".join(parts), limit=limit)
