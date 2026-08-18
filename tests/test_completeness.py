"""取得が「完全か」の判定。

`search_papers` は非 200 でページングを打ち切り、切り詰めた論文リストを
「完全な結果」として返していた。人事評価で論文数・被引用数を過小に見せる
直接の原因になるため、部分結果は必ずそうと分かるようにする。
"""
from unittest.mock import patch

import pytest

from conftest import make_response
from scopus_tools.api import PAGINATION_TOLERANCE, SCOPUS_PAGINATION_LIMIT, ScopusClient
from scopus_tools.httpcache import HttpLayer


@pytest.fixture(autouse=True)
def _no_throttle_sleep():
    """スロットルの sleep をスキップしてテストを速く保つ(間隔は別途検証済み)。"""
    with patch("time.sleep"):
        yield


def _client(max_retries=0):
    return ScopusClient(api_key="dummy_key",
                        http=HttpLayer(auth_params={"apiKey": "dummy_key"},
                                       max_retries=max_retries, enabled=False))


def _page(entries, total):
    return make_response({
        "search-results": {
            "opensearch:totalResults": str(total),
            "entry": entries,
        }
    })


def _by_start(mapping, default=None):
    """start パラメータをキーに応答を返す side_effect。

    ページ取得は並列化されており requests.get の呼び出し順が不定なので、
    側効果リスト(逐次順序前提)ではなくこれを使う。
    """
    def _side_effect(url, params=None, **kwargs):
        resp = mapping.get(params["start"], default)
        if resp is None:
            raise AssertionError(f"unexpected request for start={params['start']}")
        return resp
    return _side_effect


def _entries(start, count):
    return [{"eid": f"e{start + i}", "dc:title": f"P{start + i}",
             "prism:coverDate": "2023-01-01", "citedby-count": "1",
             "prism:publicationName": "J", "subtypeDescription": "Article",
             "author": [{"authname": "A", "authid": "111"}]}
            for i in range(count)]


class TestComplete:
    def test_single_page_is_complete(self):
        with patch("requests.get", return_value=_page(_entries(0, 3), 3)):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is True
        assert res.reason is None
        assert res.actual_total == 3
        assert res.expected_total == 3

    def test_all_pages_ok_is_complete(self):
        pages = _by_start({0: _page(_entries(0, 25), 60),
                           25: _page(_entries(25, 25), 60),
                           50: _page(_entries(50, 10), 60)})
        with patch("requests.get", side_effect=pages):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is True
        assert res.actual_total == 60
        assert res.request_count == 3

    def test_empty_result_is_complete(self):
        """0 件は「失敗」ではなく「完全に取れて 0 件」。"""
        with patch("requests.get", return_value=_page([], 0)):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is True
        assert res.actual_total == 0


class TestIncomplete:
    def test_mid_pagination_failure_is_flagged(self):
        """2 ページ目が 500 → 部分結果を返しつつ complete=False。"""
        pages = _by_start({0: _page(_entries(0, 25), 50),
                           25: make_response(status_code=500)})
        with patch("requests.get", side_effect=pages):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is False
        assert "500" in res.reason
        assert res.actual_total == 25       # 部分結果は今までどおり返す
        assert res.expected_total == 50

    def test_first_page_failure_is_flagged(self):
        with patch("requests.get", return_value=make_response(status_code=503)):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is False
        assert res.papers == []

    def test_pagination_limit_is_flagged_before_hitting_400(self):
        """Scopus は start > 5000 を拒否する。踏む前に止めて不完全と報告する。"""
        page = _page(_entries(0, 25), 9000)
        with patch("requests.get", return_value=page) as get_mock:
            res = _client().search_papers_detailed(["111"])
        assert res.complete is False
        assert str(SCOPUS_PAGINATION_LIMIT) in res.reason
        # 上限までしか投げない(そこから先を叩き続けない)
        assert get_mock.call_count == SCOPUS_PAGINATION_LIMIT // 25

    def test_short_result_versus_total_is_flagged(self):
        """総数 50 と言われたのに 25 件しか集まらなければ不完全。"""
        # 2 ページ目が空 = データ終端。不足を報告する。
        pages = _by_start({0: _page(_entries(0, 25), 50), 25: _page([], 50)})
        with patch("requests.get", side_effect=pages) as get_mock:
            res = _client().search_papers_detailed(["111"])
        assert res.complete is False
        assert "50" in res.reason
        assert get_mock.call_count == 2

    def test_small_shortfall_is_tolerated(self):
        """ページ間の重複排除で少し減るのは許容範囲(誤警告を出さない)。"""
        total = 100
        keep = int(total * PAGINATION_TOLERANCE)     # 98
        pages = _by_start({0: _page(_entries(0, 25), total),
                           25: _page(_entries(25, 25), total),
                           50: _page(_entries(50, 25), total),
                           75: _page(_entries(75, keep - 75), total)})
        with patch("requests.get", side_effect=pages):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is True

    def test_bad_total_results_is_flagged(self):
        bad = make_response({"search-results": {"opensearch:totalResults": "not-a-number",
                                                "entry": []}})
        with patch("requests.get", return_value=bad):
            res = _client().search_papers_detailed(["111"])
        assert res.complete is False
        assert "totalResults" in res.reason


class TestSelfHealing:
    """打ち切ったページングは、再実行で続きだけを取りに行く。"""

    def test_failed_run_caches_only_the_pages_it_got(self, tmp_path):
        from scopus_tools import cachedb
        db = cachedb.CacheDB(str(tmp_path / "c.sqlite3"))
        client = ScopusClient(api_key="k",
                              http=HttpLayer(db=db, auth_params={"apiKey": "k"},
                                             max_retries=0, enabled=True))
        # 100 件 = 4 ページ。2 ページ目以降が失敗。並列取得なのでインフライトの
        # 数ページは投げてしまうが、失敗ページはキャッシュされない。
        pages = _by_start({0: _page(_entries(0, 25), 100)},
                          default=make_response(status_code=500))
        with patch("requests.get", side_effect=pages) as m:
            res = client.search_papers_detailed(["111"])
        assert res.complete is False
        assert 2 <= m.call_count <= 4
        assert db.stats()["entries"] == 1  # 成功した 1 ページだけキャッシュ

    def test_rerun_fetches_only_the_missing_pages(self, tmp_path):
        from scopus_tools import cachedb
        db = cachedb.CacheDB(str(tmp_path / "c.sqlite3"))

        def _client():
            return ScopusClient(api_key="k",
                                http=HttpLayer(db=db, auth_params={"apiKey": "k"},
                                               max_retries=0, enabled=True))

        with patch("requests.get",
                   side_effect=_by_start({0: _page(_entries(0, 25), 100)},
                                         default=make_response(status_code=500))):
            _client().search_papers_detailed(["111"])

        # 再実行: 1 ページ目はキャッシュから出るので 3 回しか送らない
        rest = _by_start({25: _page(_entries(25, 25), 100),
                          50: _page(_entries(50, 25), 100),
                          75: _page(_entries(75, 25), 100)})
        with patch("requests.get", side_effect=rest) as m:
            res = _client().search_papers_detailed(["111"])
        assert m.call_count == 3
        assert res.complete is True
        assert res.actual_total == 100

        # 3 回目は完全にキャッシュ、通信ゼロ
        with patch("requests.get") as m3:
            res3 = _client().search_papers_detailed(["111"])
        m3.assert_not_called()
        assert res3.complete is True and res3.actual_total == 100


class TestBackwardCompatibility:
    def test_search_papers_still_returns_a_list(self):
        with patch("requests.get", return_value=_page(_entries(0, 2), 2)):
            papers = _client().search_papers(["111"])
        assert isinstance(papers, list) and len(papers) == 2

    def test_search_papers_returns_partials_on_failure_as_before(self):
        """既存の呼び出し側を壊さない: 例外ではなく部分結果を返す。"""
        pages = _by_start({0: _page(_entries(0, 25), 50),
                           25: make_response(status_code=500)})
        with patch("requests.get", side_effect=pages):
            papers = _client().search_papers(["111"])
        assert len(papers) == 25

    def test_incomplete_is_logged_as_error(self, caplog):
        import logging
        pages = _by_start({0: _page(_entries(0, 25), 50),
                           25: make_response(status_code=500)})
        with caplog.at_level(logging.ERROR), patch("requests.get", side_effect=pages):
            _client().search_papers_detailed(["111"])
        assert any("INCOMPLETE" in r.message or "incomplete" in r.message
                   for r in caplog.records)


class TestMcpSurfacing:
    """MCP はモデルに「全部は取れていない」と明示する。"""

    def test_incomplete_note_in_list_papers(self, monkeypatch):
        import os
        from unittest.mock import MagicMock
        from scopus_tools import mcp_server
        from scopus_tools.api import FetchResult

        client = MagicMock()
        client.search_papers_detailed.return_value = FetchResult(
            papers=[], complete=False, reason="HTTP 500 at start=25",
            request_count=2, expected_total=75, actual_total=25)
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            result = mcp_server.list_papers("111")
        assert result["incomplete"] is True
        assert "500" in result["incomplete_reason"]
        assert "full publication record" in result["incomplete_note"]
        # 「再実行すれば続きが取れる」と行動可能な指示まで含める
        assert "again" in result["incomplete_note"]
        assert "refresh=true" in result["incomplete_note"]

    def test_complete_result_has_no_incomplete_key(self, monkeypatch):
        import os
        from unittest.mock import MagicMock
        from scopus_tools import mcp_server
        from scopus_tools.api import FetchResult

        client = MagicMock()
        client.search_papers_detailed.return_value = FetchResult(papers=[], complete=True)
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
            result = mcp_server.list_papers("111")
        assert "incomplete" not in result
