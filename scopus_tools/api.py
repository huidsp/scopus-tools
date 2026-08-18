import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from scopus_tools import scie
from scopus_tools.httpcache import HttpLayer
from scopus_tools.utils import progress, progress_done

logger = logging.getLogger(__name__)

# Scopus は非カーソルのページングでは start > 5000 を拒否する。
SCOPUS_PAGINATION_LIMIT = 5000

# 取得件数 / 総件数の許容比。ページ間の重複排除と件数変動を吸収する。
PAGINATION_TOLERANCE = 0.98

# ページ並列取得のワーカー数。scopus_search の rps は 9 で、スロットル
# (安全係数込み ~7.5 req/s)は HttpLayer 側で全スレッド共有の予約制なので、
# ここは往復レイテンシを隠すだけの並列度でよい。
PAGE_FETCH_WORKERS = 4

# find_papers が API に投げる件数。limit と切り離して固定にしてある:
# キャッシュキーは全パラメータを含むので、count を可変にすると limit を変えるたびに
# 別エントリになり同じ論文を取り直してしまう。
FIND_PAGE_SIZE = 25


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


def _as_list(value):
    """Scopus は要素が 1 つのとき配列ではなく dict を返すことがある。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _cover_year(value):
    """`prism:coverDate` から発行年を取り出す。取れなければ 0。

    `entry.get("prism:coverDate", "0000")` では不十分だった。既定値が効くのは
    **キーが無いとき**だけで、Scopus が値に null や空文字を返すと `None[:4]` の
    TypeError / `int("")` の ValueError になる。これは `search_papers` の
    ページングループの中で送出されるため、1 件の欠損が著者の取得全体を落とす。
    """
    try:
        return int(str(value or "")[:4])
    except (TypeError, ValueError):
        return 0


def parse_entry(entry, author_ids=None, detail=False, include_abstract=False):
    """Scopus の 1 エントリを論文 dict に変換する。

    `author_ids` を渡すと、その著者が何番目か(`author_position`)と筆頭著者か
    (`is_first_author`)を判定する。**`author_ids=None` のときは判定できないので
    どちらも `None`** を入れる(`False` や 0 にすると「筆頭ではない」と誤読される)。

    `detail=True` で著者の Scopus ID・所属・キーワードなどを足す。既定で足さないのは、
    `list_papers` が最大 200 件返すため、1 件あたりのサイズがそのまま応答量になるから。
    抄録はさらに大きい(1 件 1,500 文字程度)ので `include_abstract` で別扱いにする。
    """
    authors = _as_list(entry.get("author"))
    auth_list = [a.get("authname") for a in authors]

    first_author_flag = None
    author_position = None
    if author_ids is not None:
        first_author_flag = bool(authors) and authors[0].get("authid") in author_ids
        for pos, a in enumerate(authors, start=1):
            if a.get("authid") in author_ids:
                author_position = pos
                break

    paper = {
        "title": entry.get("dc:title"),
        "year": _cover_year(entry.get("prism:coverDate")),
        "citations": int(entry.get("citedby-count", 0)),
        "journal": entry.get("prism:publicationName"),
        "issn": entry.get("prism:issn", ""),
        "eissn": entry.get("prism:eIssn", ""),
        "volume": entry.get("prism:volume", ""),
        "issue": entry.get("prism:issueIdentifier", ""),
        "pages": entry.get("prism:pageRange", ""),
        "aggregation_type": entry.get("prism:aggregationType", ""),
        "type": entry.get("subtypeDescription"),
        "eid": entry.get("eid"),
        "auth_list": auth_list,
        "authors": ", ".join(a for a in auth_list if a),
        "is_first_author": first_author_flag,
        "author_position": author_position,
        "author_count": len(authors),
        # 安価で価値の高いものは全経路に入れる(1 件あたり数十バイト)
        "doi": entry.get("prism:doi", ""),
        # pageRange が無く article-number だけを持つ論文がある
        "article_number": entry.get("article-number", "") or "",
        "open_access": bool(entry.get("openaccessFlag")),
    }

    if detail:
        paper["authors_detail"] = [{
            "name": a.get("authname"),
            "authid": a.get("authid"),
            "surname": a.get("surname"),
            "given_name": a.get("given-name"),
            "orcid": a.get("orcid"),
            "sequence": a.get("@seq"),
        } for a in authors]
        paper["affiliations"] = [{
            "name": af.get("affilname"),
            "city": af.get("affiliation-city"),
            "country": af.get("affiliation-country"),
            "afid": af.get("afid"),
        } for af in _as_list(entry.get("affiliation"))]
        keywords = entry.get("authkeywords") or ""
        paper["keywords"] = [k.strip() for k in keywords.split("|") if k.strip()]
        paper["source_id"] = entry.get("source-id", "")
        paper["cover_date"] = entry.get("prism:coverDate", "")
        if include_abstract:
            paper["abstract"] = entry.get("dc:description") or ""

    return paper


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

    def get_serial_metrics(self, issns):
        """ISSN 群の雑誌指標(CiteScore / SJR / SNIP)を引く。

        戻り値は `({正規化ISSN: レコード}, [取得できなかった ISSN])`。
        業績リストと違い、一部の雑誌の指標が欠けても致命的ではないので
        `FetchResult` は使わない。ただし欠けた事実は必ず返す。

        **応答は位置ではなく ISSN で突き合わせること。** 未知の ISSN はエラーに
        ならず単に返ってこない(実測: 50 件要求して 49 件)ので、順番で対応付けると
        全部ずれる。`serial.parse_serial_entry` が拾う `prism:issn` / `prism:eIssn`
        の両方をキーにする。
        """
        from scopus_tools import serial

        wanted, seen = [], set()
        for value in issns or []:
            key = scie.normalize_issn(value)
            if key and key not in seen:
                seen.add(key)
                wanted.append(key)
        if not wanted:
            return {}, []

        url = f"{self.base_url}/serial/title"
        table = {}
        for start in range(0, len(wanted), serial.BATCH_SIZE):
            chunk = wanted[start:start + serial.BATCH_SIZE]
            response = self._http.get(
                url,
                params={"issn": ",".join(chunk), "view": "CITESCORE",
                        "count": serial.BATCH_SIZE},
                headers={"Accept": "application/json"}, api="scopus_serial")
            if response.status_code != 200:
                logger.warning("Serial title request failed: status=%s body=%s",
                               response.status_code,
                               response.text[:200].replace("\n", " "))
                continue
            try:
                entries = response.json()["serial-metadata-response"].get("entry") or []
            except (ValueError, KeyError, TypeError):
                logger.warning("Unexpected serial title response for %d ISSNs", len(chunk))
                continue
            for entry in entries:
                record = serial.parse_serial_entry(entry)
                for issn in record["issns"]:
                    table[issn] = record

        missing = [i for i in wanted if i not in table]
        logger.info("Serial metrics: %d/%d ISSNs resolved (%d requests)",
                    len(wanted) - len(missing), len(wanted),
                    -(-len(wanted) // serial.BATCH_SIZE))
        return table, missing

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

    def find_papers(self, title=None, doi=None, author_last_name=None,
                    limit=10, include_abstract=False):
        """タイトル / DOI / 著者姓から論文を引く(**著者の Scopus ID 付き**)。

        ある研究者の論文が複数の Author ID に分裂しているとき、その論文を 1 件引いて
        著者一覧の `authid` を見れば、もう一方の ID が分かる。

        Scopus のタイトル照合は緩く、先頭数語や 1 語の綴り違いでも当たる(実測)。
        単一ヒットを前提にせず、複数返して呼び出し側に判断させる。
        """
        clauses = []
        if title:
            # Scopus のクエリはダブルクォートで囲むので、値の側の " は落とす
            clauses.append(f'TITLE("{str(title).replace(chr(34), " ").strip()}")')
        if doi:
            clauses.append(f'DOI("{str(doi).replace(chr(34), " ").strip()}")')
        if author_last_name:
            clauses.append(f"AUTHLASTNAME({str(author_last_name).replace(chr(34), ' ').strip()})")
        if not clauses:
            raise ValueError("find_papers requires at least one of title, doi or author_last_name")

        query = " AND ".join(clauses)
        logger.info("Finding papers: %s", query)

        # count は常に FIND_PAGE_SIZE で投げる。キャッシュキーは全パラメータを含むので、
        # limit をそのまま渡すと limit を変えるたびに別エントリになり再取得してしまう。
        response = self._http.get(
            f"{self.base_url}/search/scopus",
            params={"query": query, "count": FIND_PAGE_SIZE, "view": "COMPLETE"},
            headers={"Accept": "application/json"},
            api="scopus_search")

        if response.status_code != 200:
            reason = f"HTTP {response.status_code}"
            logger.error("find_papers failed: %s", reason)
            return FetchResult(papers=[], complete=False, reason=reason, request_count=1)

        data = response.json().get("search-results", {})
        try:
            total = int(data.get("opensearch:totalResults", 0))
        except (TypeError, ValueError):
            total = None
        entries = data.get("entry", [])
        # 0 件のとき Scopus は error を持つダミー entry を返すことがある
        entries = [e for e in entries if e.get("eid")]

        papers = [parse_entry(e, author_ids=None, detail=True,
                              include_abstract=include_abstract)
                  for e in entries]
        limit = max(1, int(limit or 10))
        complete = True
        reason = None
        if total is not None and total > len(papers):
            complete = False
            reason = (f"{total} papers matched but only the first {len(papers)} were "
                      f"retrieved; narrow the query (add --doi or --last)")
        logger.info("find_papers: %d matched, returning %d", total or 0, min(len(papers), limit))
        return FetchResult(papers=papers[:limit], complete=complete, reason=reason,
                           request_count=1, expected_total=total,
                           actual_total=min(len(papers), limit))

    def search_papers(self, author_ids, query_extra="", page_size=25):
        """論文リストを返す(既存の契約)。不完全でも部分結果を返す。

        「完全に取れたか」を知りたい呼び出し側は `search_papers_detailed` を使うこと。
        """
        return self.search_papers_detailed(author_ids, query_extra, page_size).papers

    def search_papers_detailed(self, author_ids, query_extra="", page_size=25, detail=False):
        """論文リストと **完全性の判定** を返す。

        非 200 でループを抜けた場合、Scopus のページング上限(start > 5000)に
        達した場合、件数が総数と噛み合わない場合は `complete=False` になる。
        切り詰められた論文リストを「完全な業績」として扱うと、人事評価で
        論文数・被引用数を過小に見せてしまうため、必ず呼び出し側に伝える。

        `detail=True` で各論文に共著者の Scopus ID などを付ける(応答が大きくなる)。
        """
        papers_dict = {}
        query = " OR ".join([f"AU-ID({aid})" for aid in author_ids])
        if query_extra:
            query = f"({query}) AND {query_extra}"
        logger.info("Searching papers for author_ids=%s", author_ids)

        url = f"{self.base_url}/search/scopus"

        def fetch_page(start):
            params = {
                "query": query,
                "start": start, "count": page_size, "view": "COMPLETE"
            }
            logger.debug("Requesting page: start=%d, count=%d", start, page_size)
            return self._http.get(url, params=params,
                                  headers={"Accept": "application/json"},
                                  api="scopus_search")

        complete = True
        reason = None
        failed_starts = {}    # start -> reason(最小の start を採用する)

        # 1 ページ目は同期で取り、総数を知る。
        request_count = 1
        response = fetch_page(0)
        if response.status_code != 200:
            reason = f"HTTP {response.status_code} at start=0"
            logger.error("search_papers incomplete: %s", reason)
            progress_done()
            return FetchResult(papers=[], complete=False, reason=reason,
                               request_count=1, expected_total=None, actual_total=0)
        data = response.json().get("search-results", {})
        try:
            total = int(data.get("opensearch:totalResults", 0))
        except (TypeError, ValueError):
            reason = "totalResults missing or not an integer"
            logger.error("search_papers incomplete: %s", reason)
            progress_done()
            return FetchResult(papers=[], complete=False, reason=reason,
                               request_count=1, expected_total=None, actual_total=0)
        expected_total = total
        pages = {}            # start -> entries(マージは start 順に行う)
        first_entries = data.get("entry", [])
        if first_entries:
            pages[0] = first_entries
        elif total > 0:
            failed_starts[0] = ("Scopus returned an empty page at start=0 "
                                f"but reported {total} total")

        if total > SCOPUS_PAGINATION_LIMIT:
            # Scopus は start > 5000 を拒否する。上限までで打ち切り、不完全と報告する。
            complete = False
            reason = (f"Scopus paginates only up to {SCOPUS_PAGINATION_LIMIT} records; "
                      f"{total} matched. Narrow the query (e.g. by year).")
            logger.error("search_papers incomplete: %s", reason)

        # 残りページは並列で取る。スロットル(HttpLayer 側で全スレッド共有の予約制)が
        # rps を守るので、ここは往復レイテンシを隠すだけの並列度でよい。
        starts = list(range(page_size, min(total, SCOPUS_PAGINATION_LIMIT), page_size))
        if starts and not failed_starts:
            snapshot = self._http.snapshot_collectors()
            total_pages = 1 + len(starts)

            def worker(start):
                # collect()(as-of 記録)のスタックは threading.local なので、
                # 親スレッドのものを引き継がないと取得日時が記録されない。
                self._http.adopt_collectors(snapshot)
                return start, fetch_page(start)

            done_pages = 1
            with ThreadPoolExecutor(
                    max_workers=min(PAGE_FETCH_WORKERS, len(starts))) as executor:
                futures = [executor.submit(worker, s) for s in starts]
                for fut in as_completed(futures):
                    if fut.cancelled():
                        continue
                    start, resp = fut.result()
                    request_count += 1
                    done_pages += 1
                    progress(f"  Scopus fetch: {done_pages}/{total_pages} pages "
                             f"({total} entries total)")
                    if resp.status_code != 200:
                        failed_starts[start] = f"HTTP {resp.status_code} at start={start}"
                    else:
                        entries = resp.json().get("search-results", {}).get("entry", [])
                        if entries:
                            pages[start] = entries
                        elif start < total:
                            # 空ページはデータ終端の合図。逐次実装は以降を投げなかった。
                            # 並列でも未開始のページを取り消してクォータ浪費を抑える。
                            failed_starts[start] = (
                                f"Scopus returned an empty page at start={start} "
                                f"but reported {total} total")
                    if start in failed_starts:
                        for f in futures:
                            f.cancel()

        if failed_starts:
            complete = False
            if reason is None:
                reason = failed_starts[min(failed_starts)]
            logger.error("search_papers incomplete: %s", reason)

        for start in sorted(pages):
            for e in pages[start]:
                eid = e.get("eid")
                if not eid:
                    continue

                new_entry = parse_entry(e, author_ids, detail=detail)
                authors = new_entry["auth_list"]
                first_author_flag = new_entry["is_first_author"]
                author_position = new_entry["author_position"]

                if eid in papers_dict:
                    # 重複EIDはcitationsの最大値とis_first_authorのORでマージ。
                    # author_position はより筆頭に近い(小さい)値を採用。
                    papers_dict[eid]["citations"] = max(papers_dict[eid]["citations"], new_entry["citations"])
                    papers_dict[eid]["is_first_author"] = papers_dict[eid]["is_first_author"] or first_author_flag
                    existing_pos = papers_dict[eid].get("author_position")
                    if author_position is not None and (existing_pos is None or author_position < existing_pos):
                        papers_dict[eid]["author_position"] = author_position
                    if not papers_dict[eid].get("author_count"):
                        papers_dict[eid]["author_count"] = new_entry["author_count"]
                else:
                    papers_dict[eid] = new_entry

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