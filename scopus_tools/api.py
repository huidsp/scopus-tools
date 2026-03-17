import requests
import os
import logging

logger = logging.getLogger(__name__)

class ScopusClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("SCOPUS_API_KEY")
        if not self.api_key:
            raise ValueError("SCOPUS_API_KEY is not set.")
        self.base_url = "https://api.elsevier.com/content"
        logger.debug("ScopusClient initialized.")

    def get_author_profile(self, author_id):
        url = f"{self.base_url}/author/author_id/{author_id}"
        logger.debug("Fetching author profile: author_id=%s", author_id)
        response = requests.get(url, params={"apiKey": self.api_key}, headers={"Accept": "application/json"})
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

    def search_author_by_name(self, name):
        parts = name.split()
        if len(parts) < 2:
            logger.error("Invalid name format: %s", name)
            return []

        url = f"{self.base_url}/search/author"
        seen_ids = set()
        results = []

        # Pattern 1 (first last) と Pattern 2 (last first) の両方を試みる
        patterns = [
            (parts[0], parts[-1]),   # Pattern 1: parts[0]=first, parts[-1]=last
            (parts[-1], parts[0]),   # Pattern 2: parts[0]=last,  parts[-1]=first
        ]
        for first, last in patterns:
            query = f"AUTHLASTNAME({last}) AND AUTHFIRST({first})"
            logger.debug("Searching author: AUTHLASTNAME(%s) AND AUTHFIRST(%s)", last, first)
            resp = requests.get(url, params={"query": query, "apiKey": self.api_key}, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                logger.warning("Author search failed: status=%s", resp.status_code)
                continue
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

        logger.info("Author search returned %d unique results for: %s", len(results), name)
        return results

    def search_papers(self, author_ids, query_extra="", page_size=25):
        papers_dict = {}
        query = " OR ".join([f"AU-ID({aid})" for aid in author_ids])
        if query_extra:
            query = f"({query}) AND {query_extra}"
        logger.info("Searching papers for author_ids=%s", author_ids)

        start = 0
        total = 1
        while start < total:
            url = f"{self.base_url}/search/scopus"
            params = {
                "query": query, "apiKey": self.api_key,
                "start": start, "count": page_size, "view": "COMPLETE"
            }
            logger.debug("Requesting page: start=%d, count=%d", start, page_size)
            response = requests.get(url, headers={"Accept": "application/json"}, params=params)
            if response.status_code != 200:
                logger.error("Search request failed: status=%s, start=%d", response.status_code, start)
                break

            data = response.json().get("search-results", {})
            total = int(data.get("opensearch:totalResults", 0))
            entries = data.get("entry", [])
            logger.debug("Retrieved %d entries (total=%d)", len(entries), total)

            for e in entries:
                eid = e.get("eid")
                if not eid:
                    continue

                authors = e.get("author", [])
                if isinstance(authors, dict):
                    authors = [authors]
                auth_list = [a.get("authname") for a in authors]

                first_author_flag = False
                if authors:
                    first_authid = authors[0].get("authid")
                    if first_authid in author_ids:
                        first_author_flag = True

                new_entry = {
                    "title": e.get("dc:title"),
                    "year": int(e.get("prism:coverDate", "0000")[:4]),
                    "citations": int(e.get("citedby-count", 0)),
                    "journal": e.get("prism:publicationName"),
                    "volume": e.get("prism:volume", ""),
                    "issue": e.get("prism:issueIdentifier", ""),
                    "pages": e.get("prism:pageRange", ""),
                    "aggregation_type": e.get("prism:aggregationType", ""),
                    "type": e.get("subtypeDescription"),
                    "eid": eid,
                    "auth_list": auth_list,
                    "authors": ", ".join(a for a in auth_list if a),
                    "is_first_author": first_author_flag,
                }

                if eid in papers_dict:
                    # 重複EIDはcitationsの最大値とis_first_authorのORでマージ
                    papers_dict[eid]["citations"] = max(papers_dict[eid]["citations"], new_entry["citations"])
                    papers_dict[eid]["is_first_author"] = papers_dict[eid]["is_first_author"] or first_author_flag
                else:
                    papers_dict[eid] = new_entry
            start += page_size

        logger.info("Search complete: %d unique papers found.", len(papers_dict))
        return list(papers_dict.values())

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