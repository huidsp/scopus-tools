"""取得が失敗したときに、失敗だと分かる形で返るかを固定する。

人事選考で使う以上、最も危険なのは「取れなかった」が「実績が無い」に化けること。
ここでは通信系の失敗が例外で素通しにならず、モデルが読める `error` dict になること、
および失敗を「該当なし」と誤読させる文言が付かないことを確認する。
"""
import os
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from scopus_tools import mcp_server
from scopus_tools.api import FetchResult
from scopus_tools.httpcache import OfflineError, QuotaExceeded, RateLimited


_ENV = {"SCOPUS_API_KEY": "dummy", "KAKEN_APP_ID": "dummy"}

# 取得系ツールと、最小の呼び出し引数
TOOLS = [
    ("search_author", dict(first_name="Taro", last_name="Yamada")),
    ("author_profile", dict(author_id="111")),
    ("author_summary", dict(author_ids="111")),
    ("list_papers", dict(author_ids="111")),
    ("find_papers", dict(title="x")),
]


@pytest.fixture
def scopus_raising(monkeypatch):
    """_http.get が任意の例外を投げる ScopusClient を差し込む。"""
    def install(exc):
        client = MagicMock()
        client._http.get.side_effect = exc
        client._http.refresh = False
        # 実クライアントは _http.get 越しに失敗するので、公開メソッドも同じ例外にする
        for name in ("search_author", "get_author_profile",
                     "search_papers_detailed", "find_papers"):
            getattr(client, name).side_effect = exc
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        return client
    return install


class TestNetworkFailuresBecomeErrorDicts:
    """例外のままだとホストにはプロトコルエラーとしか見えず、原因が伝わらない。"""

    @pytest.mark.parametrize("tool,kwargs", TOOLS)
    def test_quota_exhausted(self, tool, kwargs, scopus_raising):
        scopus_raising(QuotaExceeded("scopus_search", int(time.time()) + 3 * 86400))
        with patch.dict(os.environ, _ENV, clear=True):
            result = getattr(mcp_server, tool)(**kwargs)
        assert "error" in result, f"{tool} raised instead of returning an error dict"
        assert result["quota_exhausted"] is True
        # リセットまで再試行しても無駄だと明示する
        assert result["retriable"] is False
        assert "quota" in result["error"].lower()

    @pytest.mark.parametrize("tool,kwargs", TOOLS)
    def test_offline_miss(self, tool, kwargs, scopus_raising):
        scopus_raising(OfflineError("not cached"))
        with patch.dict(os.environ, _ENV, clear=True):
            result = getattr(mcp_server, tool)(**kwargs)
        assert result.get("offline") is True
        assert result["retriable"] is False

    @pytest.mark.parametrize("exc,retriable", [
        (RateLimited("throttled"), True),
        (requests.Timeout("read timed out"), True),
        (requests.ConnectionError("dns failure"), True),
    ])
    def test_transient_failures_are_marked_retriable(self, exc, retriable, scopus_raising):
        scopus_raising(exc)
        with patch.dict(os.environ, _ENV, clear=True):
            result = mcp_server.list_papers("111")
        assert result["retriable"] is retriable

    def test_kaken_quota_names_the_right_vendor(self):
        """KAKEN は NII の API。"Elsevier quota exhausted" は誤り。"""
        assert "NII" in str(QuotaExceeded("kaken_researcher"))
        assert "Elsevier" not in str(QuotaExceeded("kaken_researcher"))
        assert "Elsevier" in str(QuotaExceeded("scopus_search"))

    def test_quota_message_does_not_advertise_a_cli_flag(self, scopus_raising):
        """MCP のモデルに --offline を勧めても実行できない。"""
        scopus_raising(QuotaExceeded("scopus_search", int(time.time()) + 60))
        with patch.dict(os.environ, _ENV, clear=True):
            result = mcp_server.list_papers("111")
        assert "--offline" not in result["error"]

    def test_reset_at_text_survives_a_bad_value(self):
        """報告経路そのものが落ちると本当の原因が見えなくなる。"""
        assert QuotaExceeded("scopus_search", "not-a-number").reset_at_text is None
        assert QuotaExceeded("scopus_search", None).reset_at_text is None


class TestFailureIsNotReportedAsNoMatch:

    def _client_returning(self, fetched):
        client = MagicMock()
        client.find_papers.return_value = fetched
        client._http.refresh = False
        return client

    def test_failed_fetch_gets_no_try_fewer_words_hint(self, monkeypatch):
        """401 なのに「語数を減らして再検索を」と促すと、モデルは延々と別の
        タイトルを試し、原因(認証)にたどり着けない。"""
        monkeypatch.setattr(mcp_server, "_scopus_client", self._client_returning(
            FetchResult(papers=[], complete=False, reason="HTTP 401",
                        request_count=1, expected_total=None, actual_total=0)))
        with patch.dict(os.environ, _ENV, clear=True):
            result = mcp_server.find_papers(title="x")
        assert "hint" not in result
        assert result["incomplete"] is True

    def test_genuine_no_match_still_gets_the_hint(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_scopus_client", self._client_returning(
            FetchResult(papers=[], complete=True, reason=None,
                        request_count=1, expected_total=0, actual_total=0)))
        with patch.dict(os.environ, _ENV, clear=True):
            result = mcp_server.find_papers(title="x")
        assert "hint" in result
        assert "incomplete" not in result


class TestIncompleteNoteWording:

    def test_unknown_total_is_not_rendered_as_none(self, monkeypatch):
        client = MagicMock()
        client._http.refresh = False
        client.search_papers_detailed.return_value = FetchResult(
            papers=[], complete=False, reason="HTTP 401 at start=0",
            request_count=1, expected_total=None, actual_total=0)
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, _ENV, clear=True):
            result = mcp_server.list_papers("111")
        assert "about None" not in result["incomplete_note"]
        assert "did not report a total" in result["incomplete_note"]

    def test_single_request_lookup_does_not_claim_pagination(self, monkeypatch):
        """find_papers は 1 リクエスト。「途中のページで止まった」は事実に反する。"""
        client = MagicMock()
        client._http.refresh = False
        client.find_papers.return_value = FetchResult(
            papers=[], complete=False, reason="HTTP 401",
            request_count=1, expected_total=None, actual_total=0)
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, _ENV, clear=True):
            result = mcp_server.find_papers(title="x")
        assert "Pagination stopped" not in result["incomplete_note"]
