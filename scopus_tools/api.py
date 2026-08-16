import os
import logging

from scopus_tools.httpcache import HttpLayer
from scopus_tools.utils import progress, progress_done

logger = logging.getLogger(__name__)

# Scopus は非カーソルのページングでは start > 5000 を拒否する。
SCOPUS_PAGINATION_LIMIT = 5000

# 取得件数 / 総件数の許容比。ページ間の重複排除と件数変動を吸収する。
PAGINATION_TOLERANCE = 0.98


class FetchResult:
    """取得結果と「完全に取れたか」。

    `complete=False` の場合、`papers` は**部分結果**であり、業績の全体像として
    扱ってはいけない。従来どおり例外は投げず部分結果を返すが、呼び出し側が
    その事実を利用者に伝えられるようにする。
    """

    __slots__ = ("papers", "complete", "reason", "request_count",
                 "expected_total", "actual_total")

    def __init__(self, papers, complete, reason=None, request_count=0,
                 expected_total=None, actual_total=None):
        self.papers = papers
        self.complete = complete
        self.reason = reason
        self.request_count = request_count
        self.expected_total = expected_total
        self.actual_total = actual_total if actual_total is not None else len(papers)

    def to_dict(self):
        return {
            "complete": self.complete,
            "reason": self.reason,
            "requests": self.request_count,
            "expected_total": self.expected_total,
            "actual_total": self.actual_total,
        }


class ScopusClient:
    def __init__(self, api_key=None, http=None, context=None):
        self.api_key = api_key or os.getenv("SCOPUS_API_KEY")
        if not self.api_key:
            raise ValueError("SCOPUS_API_KEY is not set.")
        self.base_url = "https://api.elsevier.com/content"
        # apiKey は HttpLayer が保持し、キャッシュキー算出後に注入する
        # (キーがキャッシュ DB に書き込まれないようにするため)。
        auth = {"apiKey": self.api_key}
        if http is not None:
            self._http = http
        elif context is not None:
            self._http = context.layer_for(auth)
        else:
            self._http = HttpLayer(auth_params=auth)
        logger.debug("ScopusClient initialized.")

    def get_author_profile(self, author_id):
        url = f"{self.base_url}/author/author_id/{author_id}"
        logger.debug("Fetching author profile: author_id=%s", author_id)
        response = self._http.get(url, params={}, headers={"Accept": "application/json"},
                                  api="scopus_author_retrieval")
        if response.status_code != 200:
            logger.warning("Failed to fetch author profile: author_id=%s, status=%s", author_id, response.status_code)
            return None, None
        data = response.json()
        try:
            profile = data["author-retrieval-response"][0]["author-profile"]["preferred-name"]
            given, surname = profile.get("given-name", ""), profile.get("surname", "")
            logger.info("Author profile fetched: %s %s (id=%s)", given, surname, author_id)
            return given, surname
        except (KeyError, IndexError):
            logger.warning("Unexpected response structure for author_id=%s", author_id)
            return None, None

    def search_author(self, first_name, last_name):
        """姓と名を明示して著者を検索する(**1 リクエスト**)。

        Author Search は週 5,000 件と最も厳しいクォータなので、姓名の順序を
        当てるために 2 回投げることはしない。順序を外したら呼び出し側が
        入れ替えて呼び直す(MCP ならホスト側モデルが判断できる)。
        """
        first = (first_name or "").strip()
        last = (last_name or "").strip()
        if not first or not last:
            logger.error("search_author requires both first_name and last_name "
                         "(got first=%r, last=%r)", first_name, last_name)
            return []

        url = f"{self.base_url}/search/author"
        query = f"AUTHLASTNAME({last}) AND AUTHFIRST({first})"
        logger.debug("Searching author: AUTHLASTNAME(%s) AND AUTHFIRST(%s)", last, first)
        resp = self._http.get(url, params={"query": query},
                              headers={"Accept": "application/json"},
                              api="scopus_author_search")
        if resp.status_code != 200:
            logger.warning("Author search failed: status=%s", resp.status_code)
            return []

        seen_ids = set()
        results = []
        for e in resp.json().get("search-results", {}).get("entry", []):
            sid = e.get("dc:identifier", "").replace("AUTHOR_ID:", "")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            pref = e.get("preferred-name", {})
            results.append({
                "name": f"{pref.get('surname', '')} {pref.get('given-name', '')}".strip(),
                "id": sid,
                "affiliation": e.get("affiliation-current", {}).get("affiliation-name", ""),
                "doc_count": e.get("document-count", ""),
            })

        logger.info("Author search returned %d results for: %s %s", len(results), first, last)
        return results

    def split_name(self, name):
        """自由入力の氏名を (first, last) に分割する。**given surname の順を仮定**する。

        分割できなければ (None, None)。順序を判断できる呼び出し側
        (MCP のホストモデル、CLI の --first/--last)は search_author を直接使うこと。
        """
        parts = (name or "").split()
        if len(parts) < 2:
            return None, None
        return parts[0], parts[-1]

    def search_author_by_name(self, name, try_both=False):
        """自由入力の氏名で検索する薄いラッパ。

        既定は **given surname の順を仮定して 1 リクエスト**。
        `try_both=True` のときだけ従来どおり順序を入れ替えて 2 回投げる
        (クォータを 2 倍消費するので、順序が本当に不明なときだけ使う)。
        """
        first, last = self.split_name(name)
        if not first:
            logger.error("Invalid name format: %s", name)
            return []

        results = self.search_author(first, last)
        if not try_both:
            return results

        seen_ids = {r["id"] for r in results}
        for extra in self.search_author(last, first):
            if extra["id"] not in seen_ids:
                seen_ids.add(extra["id"])
                results.append(extra)
        logger.info("Author search (both orderings) returned %d unique results for: %s",
                    len(results), name)
        return results

    def search_papers(self, author_ids, query_extra="", page_size=25):
        """論文リストを返す(既存の契約)。不完全でも部分結果を返す。

        「完全に取れたか」を知りたい呼び出し側は `search_papers_detailed` を使うこと。
        """
        return self.search_papers_detailed(author_ids, query_extra, page_size).papers

    def search_papers_detailed(self, author_ids, query_extra="", page_size=25):
        """論文リストと **完全性の判定** を返す。

        非 200 でループを抜けた場合、Scopus のページング上限(start > 5000)に
        達した場合、件数が総数と噛み合わない場合は `complete=False` になる。
        切り詰められた論文リストを「完全な業績」として扱うと、人事評価で
        論文数・被引用数を過小に見せてしまうため、必ず呼び出し側に伝える。
        """
        papers_dict = {}
        query = " OR ".join([f"AU-ID({aid})" for aid in author_ids])
        if query_extra:
            query = f"({query}) AND {query_extra}"
        logger.info("Searching papers for author_ids=%s", author_ids)

        start = 0
        total = 1
        expected_total = None
        request_count = 0
        complete = True
        reason = None
        while start < total:
            if start >= SCOPUS_PAGINATION_LIMIT:
                # Scopus は start > 5000 を拒否する。ここで止めないと 400 を踏んで
                # 「完全な結果」に見える切り詰めリストを返してしまう。
                complete = False
                reason = (f"Scopus paginates only up to {SCOPUS_PAGINATION_LIMIT} records; "
                          f"{total} matched. Narrow the query (e.g. by year).")
                logger.error("search_papers incomplete: %s", reason)
                break
            url = f"{self.base_url}/search/scopus"
            params = {
                "query": query,
                "start": start, "count": page_size, "view": "COMPLETE"
            }
            logger.debug("Requesting page: start=%d, count=%d", start, page_size)
            response = self._http.get(url, params=params,
                                      headers={"Accept": "application/json"},
                                      api="scopus_search")
            request_count += 1
            if response.status_code != 200:
                complete = False
                reason = f"HTTP {response.status_code} at start={start}"
                logger.error("search_papers incomplete: %s", reason)
                break

            data = response.json().get("search-results", {})
            try:
                total = int(data.get("opensearch:totalResults", 0))
            except (TypeError, ValueError):
                complete = False
                reason = "totalResults missing or not an integer"
                logger.error("search_papers incomplete: %s", reason)
                break
            if expected_total is None:
                expected_total = total
            entries = data.get("entry", [])
            total_pages = (total + page_size - 1) // page_size if total > 0 else 0
            current_page = (start // page_size) + 1 if total_pages > 0 else 0
            fetched_records = min(start + len(entries), total) if total > 0 else 0
            progress(
                f"  Scopus fetch: page {current_page}/{total_pages} "
                f"({fetched_records}/{total} entries)"
            )
            logger.debug("Retrieved %d entries (total=%d)", len(entries), total)

            if not entries:
                # 空ページはデータ終端の合図。ここで止めないと start が total に
                # 達するまで空リクエストを投げ続けてクォータを浪費する。
                if start < total:
                    complete = False
                    reason = (f"Scopus returned an empty page at start={start} "
                              f"but reported {total} total")
                    logger.error("search_papers incomplete: %s", reason)
                break

            for e in entries:
                eid = e.get("eid")
                if not eid:
                    continue

                authors = e.get("author", [])
                if isinstance(authors, dict):
                    authors = [authors]
                auth_list = [a.get("authname") for a in authors]

                first_author_flag = False
                author_position = None
                if authors:
                    first_authid = authors[0].get("authid")
                    if first_authid in author_ids:
                        first_author_flag = True
                    # クエリした著者(複数IDなら最初に一致したもの)が何番目かを求める
                    for pos, a in enumerate(authors, start=1):
                        if a.get("authid") in author_ids:
                            author_position = pos
                            break

                new_entry = {
                    "title": e.get("dc:title"),
                    "year": int(e.get("prism:coverDate", "0000")[:4]),
                    "citations": int(e.get("citedby-count", 0)),
                    "journal": e.get("prism:publicationName"),
                    "issn": e.get("prism:issn", ""),
                    "eissn": e.get("prism:eIssn", ""),
                    "volume": e.get("prism:volume", ""),
                    "issue": e.get("prism:issueIdentifier", ""),
                    "pages": e.get("prism:pageRange", ""),
                    "aggregation_type": e.get("prism:aggregationType", ""),
                    "type": e.get("subtypeDescription"),
                    "eid": eid,
                    "auth_list": auth_list,
                    "authors": ", ".join(a for a in auth_list if a),
                    "is_first_author": first_author_flag,
                    "author_position": author_position,
                    "author_count": len(authors),
                }

                if eid in papers_dict:
                    # 重複EIDはcitationsの最大値とis_first_authorのORでマージ。
                    # author_position はより筆頭に近い(小さい)値を採用。
                    papers_dict[eid]["citations"] = max(papers_dict[eid]["citations"], new_entry["citations"])
                    papers_dict[eid]["is_first_author"] = papers_dict[eid]["is_first_author"] or first_author_flag
                    existing_pos = papers_dict[eid].get("author_position")
                    if author_position is not None and (existing_pos is None or author_position < existing_pos):
                        papers_dict[eid]["author_position"] = author_position
                    if not papers_dict[eid].get("author_count"):
                        papers_dict[eid]["author_count"] = len(authors)
                else:
                    papers_dict[eid] = new_entry
            start += page_size

        progress_done()
        papers = list(papers_dict.values())

        # 件数の突き合わせ。Scopus はページ間で重複排除と件数変動があるので
        # 少しの差は許容するが、明らかに足りないときは不完全として扱う。
        if complete and expected_total is not None:
            floor = int(expected_total * PAGINATION_TOLERANCE)
            if len(papers) < floor:
                complete = False
                reason = (f"got {len(papers)} papers but Scopus reported "
                          f"{expected_total} total")
                logger.error("search_papers incomplete: %s", reason)

        if complete:
            logger.info("Search complete: %d unique papers found.", len(papers))
        else:
            # ページングは失敗したページで打ち切る。取得済みのページは
            # キャッシュに残るので、再実行すれば続きだけを取りに行く。
            logger.error("Search INCOMPLETE (%s): returning %d papers — "
                         "do not treat this as a full publication record. "
                         "Re-run to fetch the rest (already-fetched pages come "
                         "from cache and cost no quota).",
                         reason, len(papers))
        return FetchResult(papers=papers, complete=complete, reason=reason,
                           request_count=request_count,
                           expected_total=expected_total, actual_total=len(papers))

    def get_papers_by_year(self, author_ids, start_y, end_y):
        query_extra = f"PUBYEAR > {start_y - 1} AND PUBYEAR < {end_y + 1}"
        papers = self.search_papers(author_ids, query_extra=query_extra)
        total_citations = sum(p["citations"] for p in papers)
        paper_types = {}
        for p in papers:
            pt = p.get("type") or "Unknown"
            paper_types[pt] = paper_types.get(pt, 0) + 1
        logger.info("get_papers_by_year: %d papers (%d-%d)", len(papers), start_y, end_y)
        return {
            "paper_count": len(papers),
            "total_citations": total_citations,
            **paper_types,
        }