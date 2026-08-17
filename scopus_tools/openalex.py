"""OpenAlex API クライアント。Scopus の収録範囲を補うための第 3 のデータ源。

Scopus / KAKEN と並ぶネットワーク境界で、送信は同じ `httpcache.HttpLayer` を通る
(キャッシュ・スロットル・リトライ・`--offline` がそのまま効く)。

**なぜ入れたか**
  - 認証不要で、契約機関の IP に縛られない。Scopus の API キーは機関 IP に
    紐づくため学外からは 401 になるが、OpenAlex はどこからでも引ける。
  - 国内の研究会報告が入っている。J-STAGE が DOI を振り Crossref 経由で
    流れ込むため、実測で信学技報(IEICE Technical Report)が拾えた。

**最大の注意点: OpenAlex は著者を過剰にマージする**
  Scopus の誤りが「分裂」(過小)なのに対し、OpenAlex は逆向きに間違える。
  実測(ある実在の著者プロフィール 1 件で計測):

      author.id 単独                     441 件 / h=28   ← 1935 年の論文を含む
      author.id + 所属機関の ROR          241 件          ← Scopus の 244 件と一致
      author.id + 発行年 1997-2026        365 件

  機械工学・原子核物理・ソフトウェア信頼性という無関係な 3 分野が 1 つの ID に
  同居しており、所属履歴も 26 機関・57 年にまたがっていた。
  **混在プロフィールに ORCID が 1 個付いている**ので ORCID でも除去できない。

  人事選考では過大評価の方が過小評価より危険なので(過小は本人が申告するが、
  過大は誰も指摘しないまま通る)、`author_works` は絞り込み条件なしでは
  呼べないようにし、返り値に必ず `merge_risk` を載せる。
"""

import logging
import re

from scopus_tools.httpcache import HttpLayer

logger = logging.getLogger(__name__)

BASE = "https://api.openalex.org"

# 1 ページの上限は 200(OpenAlex 仕様)
PAGE_SIZE = 200

# 1 回の取得で辿るページ数の上限。200 x 25 = 5,000 件。
MAX_PAGES = 25

# 著者検索で返す候補数
AUTHOR_PAGE_SIZE = 10

_ID_RE = re.compile(r"^[AWSI]\d+$", re.IGNORECASE)


def normalize_author_id(value):
    """`A5000000001` でも `https://openalex.org/A5000000001` でも受ける。"""
    if not value:
        return ""
    s = str(value).strip().rstrip("/")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s.upper() if _ID_RE.match(s) else s


def normalize_ror(value):
    """ROR ID を `https://ror.org/xxxxx` 形式に揃える。"""
    if not value:
        return ""
    s = str(value).strip().rstrip("/")
    if s.startswith("http"):
        return s
    return f"https://ror.org/{s.lstrip('/')}"


def _reconstruct_abstract(index):
    """OpenAlex は抄録を転置インデックスで返すので、語順に戻す。"""
    if not isinstance(index, dict) or not index:
        return ""
    positions = []
    for word, spots in index.items():
        for p in spots or []:
            positions.append((p, word))
    if not positions:
        return ""
    positions.sort()
    return " ".join(w for _, w in positions)


def parse_work(work, author_ids=None, include_abstract=False):
    """OpenAlex の work を、Scopus 側 (`api.parse_entry`) と**同じ形**の dict にする。

    形を揃えるのは `core.summarize_papers` / `scie.annotate_papers_indexes` /
    `utils.print_papers_list` をそのまま通すため。出所が混ざらないよう
    `source` キーで区別する。
    """
    author_ids = {normalize_author_id(a) for a in (author_ids or []) if a}

    authorships = work.get("authorships") or []
    auth_list, authors_detail = [], []
    for pos, a in enumerate(authorships, start=1):
        person = a.get("author") or {}
        name = person.get("display_name") or ""
        auth_list.append(name)
        authors_detail.append({
            "name": name,
            "authid": normalize_author_id(person.get("id")),
            "orcid": person.get("orcid") or "",
            "sequence": pos,
            "institutions": [i.get("display_name") for i in (a.get("institutions") or [])],
        })

    is_first, position = None, None
    if author_ids:
        ids = [d["authid"] for d in authors_detail]
        is_first = bool(ids) and ids[0] in author_ids
        for pos, aid in enumerate(ids, start=1):
            if aid in author_ids:
                position = pos
                break

    location = work.get("primary_location") or {}
    src = location.get("source") or {}
    issns = src.get("issn") or []
    biblio = work.get("biblio") or {}

    paper = {
        "title": work.get("display_name") or work.get("title"),
        "year": int(work.get("publication_year") or 0),
        "citations": int(work.get("cited_by_count") or 0),
        "journal": src.get("display_name") or "",
        "issn": src.get("issn_l") or (issns[0] if issns else ""),
        "eissn": issns[1] if len(issns) > 1 else "",
        "volume": biblio.get("volume") or "",
        "issue": biblio.get("issue") or "",
        "pages": (f"{biblio['first_page']}-{biblio['last_page']}"
                  if biblio.get("first_page") and biblio.get("last_page") else ""),
        "aggregation_type": src.get("type") or "",
        "type": work.get("type") or "",
        "eid": "",                                  # Scopus 固有。突き合わせは doi で行う
        "openalex_id": normalize_author_id(work.get("id")),
        "auth_list": auth_list,
        "authors": ", ".join(a for a in auth_list if a),
        "is_first_author": is_first,
        "author_position": position,
        "author_count": len(authorships),
        "doi": (work.get("doi") or "").replace("https://doi.org/", ""),
        "article_number": "",
        "open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "authors_detail": authors_detail,
        "source": "openalex",
    }
    if include_abstract:
        paper["abstract"] = _reconstruct_abstract(work.get("abstract_inverted_index"))
    return paper


def parse_author(item):
    """OpenAlex の author を、過剰マージを判断できる形にして返す。"""
    stats = item.get("summary_stats") or {}
    affiliations = []
    for af in item.get("affiliations") or []:
        inst = af.get("institution") or {}
        years = sorted(af.get("years") or [], reverse=True)
        affiliations.append({
            "name": inst.get("display_name") or "",
            "ror": inst.get("ror") or "",
            "country": inst.get("country_code") or "",
            "years": years,
        })
    last = item.get("last_known_institutions") or []
    return {
        "author_id": normalize_author_id(item.get("id")),
        "name": item.get("display_name") or "",
        "orcid": item.get("orcid") or "",
        "works_count": int(item.get("works_count") or 0),
        "cited_by_count": int(item.get("cited_by_count") or 0),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "affiliation": (last[0].get("display_name") if last else ""),
        "affiliations": affiliations,
        "merge_risk": merge_risk(affiliations),
    }


def merge_risk(affiliations):
    """所属履歴から「複数人が 1 ID に同居している疑い」を評価する。

    決め手は所属機関の数と在籍年の広がり。1 人の研究者が 5 機関を渡り歩くことも
    あるので断定はしない — モデルに確認を促すための材料として返す。
    """
    names = {a["name"] for a in affiliations if a.get("name")}
    years = sorted({y for a in affiliations for y in (a.get("years") or [])})
    span = (years[-1] - years[0]) if len(years) >= 2 else 0
    if len(names) >= 4 or span >= 40:
        level = "high"
    elif len(names) >= 3 or span >= 25:
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "institution_count": len(names),
        "year_span": span,
        "earliest_year": years[0] if years else None,
        "note": ("OpenAlex merges same-name authors aggressively. "
                 f"This profile spans {len(names)} institutions over {span} years. "
                 "Narrow with institution_ror and/or year_range before using the counts; "
                 "works_count / h_index above are for the UNFILTERED profile."),
    }


class OpenAlexClient:
    """OpenAlex への唯一のネットワーク境界。

    `mailto` は polite pool (10 req/s) に入るための連絡先。秘密ではないが個人情報
    なので、`auth_params` 経由で **キャッシュキー算出後に注入**する。こうすると
    キャッシュ DB にメールアドレスが残らず、キャッシュキーも mailto の有無で
    変わらない(同じ応答を二重に持たない)。
    """

    def __init__(self, mailto=None, http=None, context=None):
        self.mailto = (mailto or "").strip()
        auth = {"mailto": self.mailto} if self.mailto else {}
        if http is not None:
            self._http = http
        elif context is not None:
            self._http = context.layer_for(auth)
        else:
            self._http = HttpLayer(auth_params=auth)
        if not self.mailto:
            logger.info("OpenAlex: no mailto set — using the common pool "
                        "(slower and rate-limited more aggressively).")

    # ---- 低レベル ----------------------------------------------------

    def _get(self, path, params, api):
        resp = self._http.get(f"{BASE}/{path}", params=params,
                              headers={"User-Agent": "scopus-tools/openalex-client"},
                              api=api)
        if resp.status_code != 200:
            logger.warning("OpenAlex %s failed: status=%s body=%s",
                           path, resp.status_code, resp.text[:200].replace("\n", " "))
            return None
        try:
            return resp.json()
        except ValueError:
            logger.warning("OpenAlex %s returned non-JSON", path)
            return None

    # ---- 著者 --------------------------------------------------------

    def search_author(self, name, institution_ror=None, limit=AUTHOR_PAGE_SIZE):
        """氏名で著者候補を引く。`institution_ror` を渡すと在籍機関で絞る。

        機関名の部分一致(`display_name.search`)は authors では使えないので ROR で
        指定する。ROR の方が正確でもある(表記ゆれの影響を受けない)。
        """
        params = {"search": name, "per-page": min(int(limit or 10), 50)}
        if institution_ror:
            params["filter"] = (
                f"affiliations.institution.ror:{normalize_ror(institution_ror)}")
        data = self._get("authors", params, api="openalex_author")
        if not data:
            return []
        return [parse_author(a) for a in data.get("results") or []]

    def get_author(self, author_id):
        data = self._get(f"authors/{normalize_author_id(author_id)}", {},
                         api="openalex_author")
        return parse_author(data) if data else None

    # ---- 業績 --------------------------------------------------------

    def author_works(self, author_id, institution_ror=None, year_range=None,
                     limit=1000, include_abstract=False):
        """著者の業績を引く。**絞り込み条件が 1 つも無いと ValueError。**

        裸の author.id は他人の業績を含みうる(モジュール冒頭の実測を参照)。
        人事選考で過大な数字を出さないよう、ここで機械的に禁じる。
        """
        author_id = normalize_author_id(author_id)
        if not author_id:
            raise ValueError("author_id is required")
        if not institution_ror and not year_range:
            raise ValueError(
                "author_works requires institution_ror and/or year_range. "
                "OpenAlex merges same-name authors, so an unfiltered author.id can "
                "return other people's publications (measured: 441 works including a "
                "1935 paper, versus 241 once filtered by institution).")

        filters = [f"author.id:{author_id}"]
        if institution_ror:
            filters.append(f"institutions.ror:{normalize_ror(institution_ror)}")
        if year_range:
            start_y, end_y = year_range
            filters.append(f"publication_year:{int(start_y)}-{int(end_y)}")

        return self._paged_works(",".join(filters), limit=limit,
                                 include_abstract=include_abstract,
                                 author_ids=[author_id])

    def find_paper(self, doi=None, title=None, limit=10, include_abstract=False):
        """DOI またはタイトルから論文を引く(著者 ID 付き)。

        Scopus の `find_papers` と同じ用途 — 1 件引いて著者 ID を読み取る。
        DOI は完全一致なので確実。
        """
        if not doi and not title:
            raise ValueError("give at least one of doi / title")
        if doi:
            clean = str(doi).strip().replace("https://doi.org/", "")
            filt = f"doi:https://doi.org/{clean}"
            params = {"filter": filt, "per-page": min(int(limit or 10), PAGE_SIZE)}
        else:
            params = {"filter": f"title.search:{title}",
                      "per-page": min(int(limit or 10), PAGE_SIZE)}
        data = self._get("works", params, api="openalex_work")
        if not data:
            return {"papers": [], "total_count": None, "complete": False,
                    "reason": "request failed"}
        results = data.get("results") or []
        return {
            "papers": [parse_work(w, include_abstract=include_abstract) for w in results],
            "total_count": (data.get("meta") or {}).get("count"),
            "complete": True,
            "reason": None,
        }

    # ---- ページング --------------------------------------------------

    def _paged_works(self, filter_expr, limit, include_abstract, author_ids):
        """cursor ページングで works を集める。

        戻り値は `find_paper` と同じ形。`complete=False` は「OpenAlex から全部
        取れなかった」という意味で、Scopus 側 `FetchResult.complete` と同じ扱い。
        """
        papers, cursor, pages = [], "*", 0
        expected = None
        while cursor and pages < MAX_PAGES:
            data = self._get("works", {
                "filter": filter_expr,
                "per-page": PAGE_SIZE,
                "cursor": cursor,
            }, api="openalex_work")
            if data is None:
                return {"papers": papers, "total_count": expected, "complete": False,
                        "reason": f"request failed at page {pages + 1}"}
            meta = data.get("meta") or {}
            if expected is None:
                expected = meta.get("count")
            results = data.get("results") or []
            papers.extend(parse_work(w, author_ids=author_ids,
                                     include_abstract=include_abstract)
                          for w in results)
            pages += 1
            cursor = meta.get("next_cursor")
            if not results or (limit and len(papers) >= limit):
                break

        truncated_by_pages = bool(cursor) and pages >= MAX_PAGES
        complete = not truncated_by_pages
        reason = (f"stopped at the {MAX_PAGES}-page ceiling ({MAX_PAGES * PAGE_SIZE} works)"
                  if truncated_by_pages else None)
        if expected is not None and not truncated_by_pages and len(papers) < expected:
            # limit で切ったのは不完全ではない。limit 未満なのに足りなければ異常。
            if not limit or expected <= limit:
                complete = False
                reason = f"got {len(papers)} of {expected} works"
        return {"papers": papers[:limit] if limit else papers,
                "total_count": expected, "complete": complete, "reason": reason}
