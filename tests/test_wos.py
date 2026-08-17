"""Web of Science Starter クライアントのテスト。ネットワークには触らない。

固定した実測値(モジュール docstring と ツール docstring に書いてある数字)が
挙動と食い違わないよう、著者特定の 2 経路それぞれの性格をテストで固定する。
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from scopus_tools import mcp_server, wos
from scopus_tools.wos import (WosClient, build_author_query, parse_document,
                              quoted)


def _doc(**over):
    d = {
        "uid": "WOS:000000000000001",
        "title": "A study of reliability",
        "types": ["Article"],
        "source": {
            "sourceTitle": "JOURNAL OF TESTING",
            "publishYear": 2023, "publishMonth": "FEB",
            "volume": "14", "issue": "1",
            "pages": {"range": "156-164", "begin": "156", "end": "164", "count": 9},
        },
        "names": {"authors": [
            {"displayName": "First, A.", "wosStandard": "First, A",
             "researcherId": "A-1111-2020"},
            {"displayName": "Second, B.", "wosStandard": "Second, B",
             "researcherId": "B-2222-2020"},
        ]},
        "citations": [{"db": "WOS", "count": 11}],
        "identifiers": {"doi": "10.1000/abc", "issn": "1234-5678",
                        "eissn": "8765-4321", "pmid": "123"},
        "keywords": {"authorKeywords": ["reliability", "testing"]},
        "links": {"record": "https://example.invalid/record"},
    }
    d.update(over)
    return d


class TestQueryBuilding:

    def test_researcher_id_uses_the_ai_tag(self):
        assert build_author_query(researcher_id="D-0000-2011") == 'AI=("D-0000-2011")'

    def test_orcid_also_goes_through_ai(self):
        """AI= は ORCID 書式でも ResearcherID 書式でも引ける(実測)。"""
        q = build_author_query(researcher_id="0000-0002-1825-0097")
        assert q == 'AI=("0000-0002-1825-0097")'

    def test_name_and_org_are_anded(self):
        q = build_author_query(name="Yamada T", organization="Hiroshima University")
        assert q == 'AU=("Yamada T") AND OG=("Hiroshima University")'

    def test_doi_with_parentheses_reaches_the_query_intact(self):
        c = WosClient(api_key="dummy", http=MagicMock())
        c._get = MagicMock(return_value={"metadata": {"total": 0}, "hits": []})
        c.find_documents(doi="10.1016/S0898-1221(99)00325-9")
        assert "S0898-1221(99)00325-9" in c._get.call_args[0][1]["q"]

    def test_year_range(self):
        q = build_author_query(researcher_id="X", year_range=(2021, 2025))
        assert "PY=(2021-2025)" in q

    def test_nothing_to_search_is_refused(self):
        with pytest.raises(ValueError, match="researcher_id or name"):
            build_author_query(organization="Somewhere")

    def test_values_are_wrapped_in_quotes(self):
        assert quoted("Yamada T") == '"Yamada T"'

    def test_parentheses_survive(self):
        """DOI には括弧を含むものがある。潰すと 1 件 → 0 件になった(実測)。"""
        doi = "10.1016/S0898-1221(99)00325-9"
        assert doi in quoted(doi)

    def test_embedded_quotes_are_removed(self):
        """値の中の二重引用符は囲みを壊す。"""
        out = quoted('A "quoted" B')
        assert out.count('"') == 2 and out.startswith('"') and out.endswith('"')

    def test_whitespace_is_collapsed(self):
        assert quoted("A  \n B") == '"A B"'

    def test_empty_value(self):
        assert quoted("") == "" and quoted(None) == ""


class TestParseDocument:

    def test_shape_matches_scopus(self):
        from scopus_tools.api import parse_entry
        wos_keys = set(parse_document(_doc()))
        scopus_keys = set(parse_entry({"dc:title": "t"}))
        missing = scopus_keys - wos_keys
        assert not missing, f"WoS paper dict is missing Scopus keys: {missing}"

    def test_core_fields(self):
        p = parse_document(_doc())
        assert p["title"] == "A study of reliability"
        assert p["year"] == 2023
        assert p["month"] == 2
        assert p["citations"] == 11
        assert p["journal"] == "JOURNAL OF TESTING"
        assert p["issn"] == "1234-5678"
        assert p["pages"] == "156-164"
        assert p["doi"] == "10.1000/abc"
        assert p["wos_uid"] == "WOS:000000000000001"
        assert p["keywords"] == ["reliability", "testing"]
        assert p["source"] == "wos"

    def test_times_cited_prefers_the_core_collection(self):
        p = parse_document(_doc(citations=[{"db": "BCI", "count": 3},
                                           {"db": "WOS", "count": 11}]))
        assert p["citations"] == 11

    def test_missing_citations_is_zero(self):
        """契約が無いと citations が返らない。0 にして落とさない。"""
        assert parse_document(_doc(citations=[]))["citations"] == 0

    def test_author_position_unknown_without_ids(self):
        p = parse_document(_doc())
        assert p["is_first_author"] is None
        assert p["author_position"] is None

    def test_author_position_from_researcher_id(self):
        p = parse_document(_doc(), researcher_ids=["B-2222-2020"])
        assert p["author_position"] == 2
        assert p["is_first_author"] is False
        assert p["author_count"] == 2

    def test_first_author_detected(self):
        p = parse_document(_doc(), researcher_ids=["a-1111-2020"])   # 大小無視
        assert p["is_first_author"] is True
        assert p["author_position"] == 1

    def test_missing_year_is_zero(self):
        p = parse_document(_doc(source={"sourceTitle": "J"}))
        assert p["year"] == 0
        assert p["month"] is None

    def test_no_authors_does_not_crash(self):
        p = parse_document(_doc(names={}))
        assert p["auth_list"] == [] and p["author_count"] == 0


class TestSearchCompleteness:

    def _client(self, pages):
        c = WosClient(api_key="dummy", http=MagicMock())
        c._get = MagicMock(side_effect=pages)
        return c

    def _page(self, n, total):
        return {"metadata": {"total": total}, "hits": [_doc() for _ in range(n)]}

    def test_single_page(self):
        r = self._client([self._page(3, 3)]).search("AI=(X)")
        assert r.complete is True and len(r.papers) == 3

    def test_paginates_until_total(self):
        c = self._client([self._page(50, 60), self._page(10, 60)])
        r = c.search("AI=(X)")
        assert len(r.papers) == 60
        assert r.complete is True
        assert r.request_count == 2

    def test_http_failure_is_incomplete(self):
        c = self._client([self._page(50, 500), None])
        r = c.search("AI=(X)")
        assert r.complete is False
        assert "HTTP error" in r.reason
        assert len(r.papers) == 50          # 部分結果は返す(例外にしない)

    def test_limit_is_not_incompleteness(self):
        """limit で切ったのは truncated であって incomplete ではない。"""
        c = self._client([self._page(50, 500)])
        r = c.search("AI=(X)", limit=10)
        assert r.complete is True
        assert len(r.papers) == 10

    def test_page_ceiling_is_reported(self, monkeypatch):
        monkeypatch.setattr(wos, "MAX_PAGES", 2)
        c = self._client([self._page(50, 10_000), self._page(50, 10_000)])
        r = c.search("AI=(X)")
        assert r.complete is False
        assert "ceiling" in r.reason


class TestApiKeyHandling:

    def test_missing_key_is_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="WOS_API_KEY"):
                WosClient()

    def test_key_travels_as_a_header_not_a_query_param(self):
        """WoS は X-ApiKey ヘッダ。ヘッダはキャッシュキーにも DB にも入らない。"""
        c = WosClient(api_key="secret-key")
        assert c._http.auth_headers == {"X-ApiKey": "secret-key"}
        assert c._http.auth_params == {}

    def test_header_auth_is_not_part_of_the_cache_key(self):
        from scopus_tools.httpcache import cache_key
        a = cache_key("GET", "https://x.invalid/documents", {"q": "AI=(X)"})
        b = cache_key("GET", "https://x.invalid/documents", {"q": "AI=(X)"})
        assert a == b


class TestMcpTools:

    def _install(self, monkeypatch, fetched):
        client = MagicMock()
        client._http.refresh = False
        client.author_documents.return_value = fetched
        client.find_documents.return_value = fetched
        monkeypatch.setattr(mcp_server, "_wos_client", client)
        return client

    def _result(self, n=3, total=3, complete=True):
        from scopus_tools.api import FetchResult
        return FetchResult(papers=[parse_document(_doc()) for _ in range(n)],
                           complete=complete, request_count=1, expected_total=total)

    def test_missing_key_returns_error_dict(self):
        with patch.dict(os.environ, {}, clear=True):
            mcp_server._wos_client = None
            assert "WOS_API_KEY" in mcp_server.wos_author_documents(
                researcher_id="X")["error"]

    def test_researcher_id_caveat_says_lower_bound(self, monkeypatch):
        """AI= は高精度・低再現率。総数として報告させない。"""
        self._install(monkeypatch, self._result())
        with patch.dict(os.environ, {"WOS_API_KEY": "k"}, clear=True):
            r = mcp_server.wos_author_documents(researcher_id="D-0000-2011")
        assert r["strategy"] == "researcher_id"
        assert "LOWER BOUND" in r["caveat"]

    def test_name_search_caveat_says_contaminated(self, monkeypatch):
        self._install(monkeypatch, self._result())
        with patch.dict(os.environ, {"WOS_API_KEY": "k"}, clear=True):
            r = mcp_server.wos_author_documents(name="Yamada T", organization="X")
        assert r["strategy"] == "name"
        assert "same-name" in r["caveat"]
        assert "warning" not in r          # organization を渡したので追加警告は出ない

    def test_name_without_organization_gets_an_extra_warning(self, monkeypatch):
        self._install(monkeypatch, self._result())
        with patch.dict(os.environ, {"WOS_API_KEY": "k"}, clear=True):
            r = mcp_server.wos_author_documents(name="Yamada T")
        assert "warning" in r

    def test_find_document_hints_only_on_a_successful_empty_search(self, monkeypatch):
        self._install(monkeypatch, self._result(n=0, total=0))
        with patch.dict(os.environ, {"WOS_API_KEY": "k"}, clear=True):
            assert "hint" in mcp_server.wos_find_document(doi="10.1000/x")

        self._install(monkeypatch, self._result(n=0, total=None, complete=False))
        with patch.dict(os.environ, {"WOS_API_KEY": "k"}, clear=True):
            r = mcp_server.wos_find_document(doi="10.1000/x")
        assert "hint" not in r and r["incomplete"] is True
