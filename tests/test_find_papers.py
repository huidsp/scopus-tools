"""タイトル / DOI から論文を引く find_papers と、エントリのパース。

主な用途は **分裂した Scopus Author ID の特定**。ある研究者の論文が複数の
Author ID に分かれているとき、論文を 1 件引いて著者の authid を読み取る。
"""
from unittest.mock import patch

import pytest

from conftest import make_response
from scopus_tools.api import FIND_PAGE_SIZE, ScopusClient, parse_entry
from scopus_tools.httpcache import HttpLayer


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("time.sleep"):
        yield


def _client():
    return ScopusClient(api_key="dummy",
                        http=HttpLayer(auth_params={"apiKey": "dummy"},
                                       max_retries=0, enabled=False))


ENTRY = {
    "eid": "2-s2.0-85201630348",
    "dc:title": "A Study of Something Interesting",
    "prism:coverDate": "2024-08-15",
    "prism:publicationName": "Journal of Example Studies",
    "prism:issn": "12345678",
    "prism:eIssn": "87654321",
    "prism:volume": "26",
    "prism:issueIdentifier": "",
    "prism:pageRange": "100-110",
    "prism:doi": "10.0000/example.2024.1",
    "prism:aggregationType": "Journal",
    "subtypeDescription": "Article",
    "citedby-count": "6",
    "openaccessFlag": True,
    "source-id": "99999",
    "authkeywords": "alpha | beta topic | gamma method",
    "dc:description": "An abstract that is quite long in reality.",
    "author": [
        {"authname": "First A.", "authid": "10000000001", "surname": "First",
         "given-name": "Alice", "orcid": "0000-0000-0000-0001", "@seq": "1"},
        {"authname": "Second B.", "authid": "10000000002", "surname": "Second",
         "given-name": "Bob", "orcid": "0000-0000-0000-0002", "@seq": "2"},
    ],
    "affiliation": [
        {"affilname": "Example University", "affiliation-city": "Exampleville",
         "affiliation-country": "Japan", "afid": "60000001"},
    ],
}


def _search_response(entries, total=None):
    return make_response({
        "search-results": {
            "opensearch:totalResults": str(total if total is not None else len(entries)),
            "entry": entries,
        }
    })


class TestParseEntry:
    def test_author_ids_none_leaves_position_unknown(self):
        """誰の論文か分からないときに False/0 を入れると「筆頭ではない」と誤読される。"""
        p = parse_entry(ENTRY, author_ids=None)
        assert p["is_first_author"] is None
        assert p["author_position"] is None
        assert p["author_count"] == 2

    def test_author_ids_given_computes_position(self):
        p = parse_entry(ENTRY, author_ids={"10000000002"})
        assert p["is_first_author"] is False    # 2 番目なので筆頭ではない
        assert p["author_position"] == 2

        first = parse_entry(ENTRY, author_ids={"10000000001"})
        assert first["is_first_author"] is True
        assert first["author_position"] == 1

    def test_cheap_fields_are_always_present(self):
        p = parse_entry(ENTRY, author_ids=None)
        assert p["doi"] == "10.0000/example.2024.1"
        assert p["open_access"] is True
        assert "article_number" in p

    def test_article_number_when_pages_absent(self):
        entry = dict(ENTRY, **{"prism:pageRange": None, "article-number": "299"})
        p = parse_entry(entry, author_ids=None)
        assert p["article_number"] == "299"

    def test_detail_off_omits_expensive_fields(self):
        """list_papers は最大 200 件返すので、既定で重いものを入れない。"""
        p = parse_entry(ENTRY, author_ids=None, detail=False)
        for key in ("authors_detail", "affiliations", "keywords", "abstract"):
            assert key not in p

    def test_detail_on_includes_author_ids(self):
        p = parse_entry(ENTRY, author_ids=None, detail=True)
        assert [a["authid"] for a in p["authors_detail"]] == ["10000000001", "10000000002"]
        assert p["authors_detail"][1]["orcid"] == "0000-0000-0000-0002"
        assert p["authors_detail"][1]["given_name"] == "Bob"

    def test_detail_on_includes_journal_and_affiliations(self):
        p = parse_entry(ENTRY, author_ids=None, detail=True)
        assert p["affiliations"][0]["name"] == "Example University"
        assert p["affiliations"][0]["country"] == "Japan"
        assert p["keywords"] == ["alpha", "beta topic", "gamma method"]
        assert p["source_id"] == "99999"

    def test_abstract_is_opt_in(self):
        assert "abstract" not in parse_entry(ENTRY, detail=True)
        assert "abstract" in parse_entry(ENTRY, detail=True, include_abstract=True)

    def test_legacy_author_fields_unchanged(self):
        """auth_list / authors は表示・CSV・既存テストが依存している。"""
        p = parse_entry(ENTRY, author_ids=None)
        assert p["auth_list"] == ["First A.", "Second B."]
        assert p["authors"] == "First A., Second B."

    def test_single_author_returned_as_dict(self):
        """Scopus は要素が 1 つのとき配列でなく dict を返すことがある。"""
        entry = dict(ENTRY, author={"authname": "Solo S.", "authid": "1"})
        p = parse_entry(entry, author_ids=None, detail=True)
        assert p["author_count"] == 1
        assert p["authors_detail"][0]["authid"] == "1"


class TestQueryBuilding:
    def _query(self, get_mock):
        return get_mock.call_args.kwargs["params"]["query"]

    def test_title_only(self):
        with patch("requests.get", return_value=_search_response([ENTRY])) as m:
            _client().find_papers(title="Some Title")
        assert self._query(m) == 'TITLE("Some Title")'

    def test_doi_only(self):
        with patch("requests.get", return_value=_search_response([ENTRY])) as m:
            _client().find_papers(doi="10.0000/example.2024.1")
        assert self._query(m) == 'DOI("10.0000/example.2024.1")'

    def test_author_last_name_only(self):
        with patch("requests.get", return_value=_search_response([ENTRY])) as m:
            _client().find_papers(author_last_name="Yu")
        assert self._query(m) == "AUTHLASTNAME(Yu)"

    def test_criteria_are_anded(self):
        with patch("requests.get", return_value=_search_response([ENTRY])) as m:
            _client().find_papers(title="T", doi="D", author_last_name="Yu")
        assert self._query(m) == 'TITLE("T") AND DOI("D") AND AUTHLASTNAME(Yu)'

    def test_quotes_in_input_do_not_break_the_query(self):
        with patch("requests.get", return_value=_search_response([])) as m:
            _client().find_papers(title='A "quoted" title')
        query = self._query(m)
        assert query.count('"') == 2      # 外側の 1 組だけ
        assert "quoted" in query

    def test_no_criteria_raises(self):
        with patch("requests.get") as m:
            with pytest.raises(ValueError, match="at least one"):
                _client().find_papers()
        m.assert_not_called()

    def test_count_is_fixed_so_limit_does_not_split_the_cache(self):
        """count を可変にすると limit を変えるたびに別キャッシュになる。"""
        for limit in (1, 5, 50):
            with patch("requests.get", return_value=_search_response([ENTRY])) as m:
                _client().find_papers(title="T", limit=limit)
            assert m.call_args.kwargs["params"]["count"] == FIND_PAGE_SIZE

    def test_uses_complete_view(self):
        with patch("requests.get", return_value=_search_response([ENTRY])) as m:
            _client().find_papers(title="T")
        assert m.call_args.kwargs["params"]["view"] == "COMPLETE"


class TestFindPapersResult:
    def test_returns_detailed_papers(self):
        with patch("requests.get", return_value=_search_response([ENTRY])):
            res = _client().find_papers(title="T")
        assert res.complete is True
        assert res.papers[0]["authors_detail"][1]["authid"] == "10000000002"

    def test_limit_slices_locally(self):
        entries = [dict(ENTRY, eid=f"e{i}") for i in range(10)]
        with patch("requests.get", return_value=_search_response(entries)):
            res = _client().find_papers(title="T", limit=3)
        assert len(res.papers) == 3
        assert res.expected_total == 10

    def test_more_matches_than_retrieved_is_incomplete(self):
        entries = [dict(ENTRY, eid=f"e{i}") for i in range(25)]
        with patch("requests.get", return_value=_search_response(entries, total=219008)):
            res = _client().find_papers(title="deep learning", limit=5)
        assert res.complete is False
        assert "219008" in res.reason
        assert len(res.papers) == 5

    def test_no_match_returns_empty_and_complete(self):
        with patch("requests.get", return_value=_search_response([])):
            res = _client().find_papers(title="xyzzy no such paper")
        assert res.papers == []
        assert res.complete is True

    def test_entries_without_eid_are_dropped(self):
        """0 件のとき Scopus が error を持つダミー entry を返すことがある。"""
        with patch("requests.get",
                   return_value=_search_response([{"error": "Result set was empty"}], total=0)):
            res = _client().find_papers(title="nothing")
        assert res.papers == []

    def test_http_error_is_reported(self):
        with patch("requests.get", return_value=make_response(status_code=500)):
            res = _client().find_papers(title="T")
        assert res.complete is False
        assert "500" in res.reason

    def test_goes_through_the_cache_layer(self):
        """キャッシュ・スロットル・429 リトライを既存の HttpLayer に任せている。"""
        with patch("requests.get", return_value=_search_response([ENTRY])) as m:
            _client().find_papers(title="T")
        assert m.call_args.kwargs["timeout"] is not None


class TestMcpTool:
    def _client_mock(self, papers, complete=True, reason=None, total=None):
        from unittest.mock import MagicMock
        from scopus_tools.api import FetchResult
        client = MagicMock()
        client.find_papers.return_value = FetchResult(
            papers=papers, complete=complete, reason=reason, request_count=1,
            expected_total=total if total is not None else len(papers))
        return client

    def test_returns_papers_with_as_of(self, monkeypatch):
        import os
        from scopus_tools import mcp_server
        paper = parse_entry(ENTRY, author_ids=None, detail=True)
        monkeypatch.setattr(mcp_server, "_scopus_client", self._client_mock([paper]))
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            res = mcp_server.find_papers(title="T")
        assert res["returned_count"] == 1
        assert res["papers"][0]["authors_detail"][1]["authid"] == "10000000002"
        assert "as_of" in res and "as_of_note" in res

    def test_hint_when_no_match(self, monkeypatch):
        import os
        from scopus_tools import mcp_server
        monkeypatch.setattr(mcp_server, "_scopus_client", self._client_mock([]))
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            res = mcp_server.find_papers(title="xyzzy")
        assert "DOI" in res["hint"]

    def test_incomplete_is_surfaced(self, monkeypatch):
        import os
        from scopus_tools import mcp_server
        client = self._client_mock([], complete=False, reason="219008 papers matched",
                                   total=219008)
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            res = mcp_server.find_papers(title="deep learning")
        assert res["incomplete"] is True

    def test_no_criteria_returns_error_not_exception(self, monkeypatch):
        import os
        from scopus_tools import mcp_server
        from unittest.mock import MagicMock
        client = MagicMock()
        client.find_papers.side_effect = ValueError("find_papers requires at least one of ...")
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            res = mcp_server.find_papers()
        assert "at least one" in res["error"]

    def test_missing_key_returns_error(self):
        import os
        from scopus_tools import mcp_server
        with patch.dict(os.environ, {}, clear=True):
            mcp_server._scopus_client = None
            assert "SCOPUS_API_KEY" in mcp_server.find_papers(title="T")["error"]


class TestListPapersAuthorIds:
    """list_papers は既定で著者 ID を返さない(200 件返すのでトークンに直結する)。"""

    def _mock(self, detail):
        from unittest.mock import MagicMock
        from scopus_tools.api import FetchResult
        client = MagicMock()

        def fake(ids, query_extra="", detail=False):
            return FetchResult(
                papers=[parse_entry(ENTRY, author_ids=set(ids), detail=detail)],
                complete=True, request_count=1, expected_total=1)

        client.search_papers_detailed.side_effect = fake
        return client

    def test_default_omits_author_ids(self, monkeypatch):
        import os
        from scopus_tools import mcp_server
        monkeypatch.setattr(mcp_server, "_scopus_client", self._mock(False))
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            res = mcp_server.list_papers("10000000002")
        assert "authors_detail" not in res["papers"][0]

    def test_opt_in_includes_real_author_ids(self, monkeypatch):
        import os
        from scopus_tools import mcp_server
        monkeypatch.setattr(mcp_server, "_scopus_client", self._mock(True))
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            res = mcp_server.list_papers("10000000002", include_author_ids=True)
        detail = res["papers"][0]["authors_detail"]
        assert [a["authid"] for a in detail] == ["10000000001", "10000000002"]
