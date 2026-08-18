"""MCP サーバのツール関数のユニットテスト。

MCP プロトコル層は通さず、ツール実体を直接呼んで戻り値の形を検証する。
ネットワーク境界(ScopusClient / KakenClient)はモックする。
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from scopus_tools import mcp_server


def _fetched(papers, complete=True, reason=None, expected_total=None):
    from scopus_tools.api import FetchResult
    return FetchResult(papers=papers, complete=complete, reason=reason,
                       request_count=1,
                       expected_total=expected_total if expected_total is not None else len(papers))


def _papers(n, year=2023):
    return [
        {
            "title": f"Paper {i}", "year": year - (i % 3), "citations": 10 * i,
            "journal": "J", "issn": "1234-5678", "eissn": "", "type": "Article",
            "eid": f"eid-{i}", "auth_list": ["A", "B"], "authors": "A, B",
            "is_first_author": i % 2 == 0, "author_position": 1, "author_count": 2,
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _reset_state():
    """各テストの前後でモジュールのグローバル状態を初期化する。"""
    def _clear():
        mcp_server._scopus_client = None
        mcp_server._kaken_client = None
        mcp_server._wos_client = None
        mcp_server._project_store = None
        mcp_server._PROJECTS_DIR = None
        mcp_server._INDEX_SETS = {}

    _clear()
    yield
    _clear()


@pytest.fixture
def store_dir(tmp_path):
    """プロジェクト保存先を tmp_path に向ける。"""
    mcp_server._PROJECTS_DIR = str(tmp_path / "projects")
    return mcp_server._PROJECTS_DIR


# ---------------------------------------------------------------------------
# 鍵未設定時: 例外ではなく error キーを返す
# ---------------------------------------------------------------------------

class TestMissingKeys:
    def test_scopus_tools_return_error(self):
        with patch.dict(os.environ, {}, clear=True):
            for result in (
                mcp_server.search_author("Taro", "Tanaka"),
                mcp_server.author_profile("123"),
                mcp_server.author_summary("123"),
                mcp_server.list_papers("123"),
            ):
                assert "SCOPUS_API_KEY" in result["error"]

    def test_kaken_tools_return_error(self):
        with patch.dict(os.environ, {}, clear=True):
            assert "KAKEN_APP_ID" in mcp_server.kaken_search_researcher("田中")["error"]
            assert "KAKEN_APP_ID" in mcp_server.kaken_grants("12345678")["error"]


# ---------------------------------------------------------------------------
# Scopus 系ツール
# ---------------------------------------------------------------------------

@pytest.fixture
def scopus_env():
    with patch.dict(os.environ, {"SCOPUS_API_KEY": "dummy"}, clear=True):
        yield


class TestScopusTools:
    def test_search_author(self, scopus_env):
        client = MagicMock()
        client.search_author.return_value = [
            {"name": "Tanaka T.", "id": "123", "affiliation": "Hiroshima U", "doc_count": 30}
        ]
        mcp_server._scopus_client = client
        result = mcp_server.search_author("Taro", "Tanaka")
        assert result["count"] == 1
        assert result["candidates"][0]["id"] == "123"
        assert "hint" not in result
        # 姓名を分けて 1 リクエストだけ投げる(Author Search は週 5,000 件)
        client.search_author.assert_called_once_with("Taro", "Tanaka")

    def test_search_author_hints_at_reversed_order_on_zero_hits(self, scopus_env):
        client = MagicMock()
        client.search_author.return_value = []
        mcp_server._scopus_client = client
        result = mcp_server.search_author("Tanaka", "Taro")
        assert result["count"] == 0
        assert "'Taro'" in result["hint"] and "'Tanaka'" in result["hint"]

    def test_author_profile(self, scopus_env):
        client = MagicMock()
        client.get_author_profile.return_value = ("Taro", "Tanaka")
        mcp_server._scopus_client = client
        result = mcp_server.author_profile("123")
        assert result["scopus_id"] == "123"
        assert result["first_name"] == "Taro"
        assert result["last_name"] == "Tanaka"
        # 全取得系ツールは「いつ時点のデータか」を返す
        assert "as_of" in result and "as_of_note" in result

    def test_author_summary(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(5))
        client.get_author_profile.return_value = ("Taro", "Tanaka")
        mcp_server._scopus_client = client
        result = mcp_server.author_summary("123,456", year_range="2021-2025")
        assert result["scopus_ids"] == ["123", "456"]
        assert result["year_range"] == [2021, 2025]
        assert result["summary"]["total_count"] == 5
        # ID はリストに正規化されてクライアントへ渡る
        client.search_papers_detailed.assert_called_once_with(["123", "456"])

    def test_author_summary_accepts_list_ids(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(2))
        client.get_author_profile.return_value = ("Taro", "Tanaka")
        mcp_server._scopus_client = client
        result = mcp_server.author_summary(["123", "456"])
        assert result["scopus_ids"] == ["123", "456"]

    def test_author_summary_empty_ids(self, scopus_env):
        mcp_server._scopus_client = MagicMock()
        assert "empty" in mcp_server.author_summary("")["error"]

    def test_bad_year_range_returns_error(self, scopus_env):
        mcp_server._scopus_client = MagicMock()
        assert "invalid" in mcp_server.list_papers("123", year_range="not-a-range")["error"]
        assert "<=" in mcp_server.list_papers("123", year_range="2025-2021")["error"]

    def test_list_papers_query_and_shape(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(3))
        mcp_server._scopus_client = client
        result = mcp_server.list_papers("123", year_range="2021-2025")
        assert result["year_range"] == [2021, 2025]
        assert result["total_count"] == 3
        assert result["truncated"] is False
        assert len(result["papers"]) == 3
        _, kwargs = client.search_papers_detailed.call_args
        assert kwargs["query_extra"] == "PUBYEAR > 2020 AND PUBYEAR < 2026"

    def test_list_papers_truncates(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(10))
        mcp_server._scopus_client = client
        result = mcp_server.list_papers("123", limit=4)
        assert result["total_count"] == 10
        assert result["returned_count"] == 4
        assert result["truncated"] is True
        assert len(result["papers"]) == 4

    def test_list_papers_annotates_indexes(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(3))
        mcp_server._scopus_client = client
        mcp_server._INDEX_SETS = {"SCIE": {"12345678"}}
        result = mcp_server.list_papers("123")
        assert all(p["wos_indexes"] == ["SCIE"] for p in result["papers"])

    def test_list_papers_fields_projection(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(3))
        mcp_server._scopus_client = client
        result = mcp_server.list_papers("123", fields="title, year,citations,nope")
        for p in result["papers"]:
            assert set(p.keys()) == {"title", "year", "citations"}

    def test_list_papers_sort_citations(self, scopus_env):
        client = MagicMock()
        client.search_papers_detailed.return_value = _fetched(_papers(5))
        mcp_server._scopus_client = client
        result = mcp_server.list_papers("123", sort="citations", limit=3)
        cites = [p["citations"] for p in result["papers"]]
        assert cites == sorted(cites, reverse=True)
        assert cites[0] == 40
        assert mcp_server.list_papers("123", sort="bogus")["error"]

    def test_scie_only_without_lists_errors(self, scopus_env):
        mcp_server._scopus_client = MagicMock()
        result = mcp_server.list_papers("123", scie_only=True)
        assert "scie_only" in result["error"]


# ---------------------------------------------------------------------------
# KAKEN 系ツール
# ---------------------------------------------------------------------------

class TestKakenTools:
    def test_search_and_grants(self):
        with patch.dict(os.environ, {"KAKEN_APP_ID": "dummy"}, clear=True):
            client = MagicMock()
            client.search_researcher_by_name.return_value = [{"researcher_id": "12345678"}]
            client.get_grants_by_researcher_id.return_value = [{"title": "課題A"}, {"title": "課題B"}]
            mcp_server._kaken_client = client

            found = mcp_server.kaken_search_researcher("田中太郎")
            assert found["count"] == 1

            grants = mcp_server.kaken_grants("12345678", role="principal")
            assert grants["count"] == 2
            client.get_grants_by_researcher_id.assert_called_once_with(
                "12345678", role="principal")


# ---------------------------------------------------------------------------
# KAKEN 名前マッチング
# ---------------------------------------------------------------------------

class TestLinkKakenResearcher:
    def test_single_candidate_is_adopted(self):
        with patch.dict(os.environ, {"KAKEN_APP_ID": "dummy"}, clear=True):
            client = MagicMock()
            client.search_researcher_by_name.return_value = [
                {"researcher_id": "80401243", "name": "田中 太郎", "affiliation": "Foo Univ"},
            ]
            mcp_server._kaken_client = client
            result = mcp_server.link_kaken_researcher("Taro", "Tanaka")
        assert result["researcher_ids"] == ["80401243"]

    def test_multiple_candidates_need_auto(self):
        candidates = [
            {"researcher_id": "1" * 8, "name": "田中 太郎", "affiliation": "A"},
            {"researcher_id": "2" * 8, "name": "田中 太朗", "affiliation": "B"},
        ]
        with patch.dict(os.environ, {"KAKEN_APP_ID": "dummy"}, clear=True):
            client = MagicMock()
            client.search_researcher_by_name.return_value = candidates
            mcp_server._kaken_client = client
            # auto=False なら選ばずに空を返す(モデルが候補を見て決める)
            assert mcp_server.link_kaken_researcher("Taro", "Tanaka")["researcher_ids"] == []
            # auto=True なら先頭を採用
            picked = mcp_server.link_kaken_researcher("Taro", "Tanaka", auto=True)
            assert picked["researcher_ids"] == ["1" * 8]


# ---------------------------------------------------------------------------
# プロジェクト永続化ツール
# ---------------------------------------------------------------------------

class TestProjectTools:
    def test_create_read_and_list(self, store_dir):
        created = mcp_server.create_project("選考2026")
        assert created["name"] == "選考2026"

        listed = mcp_server.list_projects()["projects"]
        assert [p["name"] for p in listed] == ["選考2026"]
        assert mcp_server.read_project("選考2026")["name"] == "選考2026"

    def test_create_rejects_duplicate(self, store_dir):
        mcp_server.create_project("P")
        assert "already exists" in mcp_server.create_project("P")["error"]

    def test_read_missing_project(self, store_dir):
        assert "not found" in mcp_server.read_project("nope")["error"]

    def test_save_researcher_section_roundtrip(self, store_dir):
        """create → save → read が往復し、ファイルに永続化される。"""
        mcp_server.create_project("選考2026")
        result = mcp_server.save_researcher_section(
            "選考2026", "田中太郎", "scopus", {"scopus_ids": ["123"], "h_index": 12})
        assert result["researcher"] == "田中太郎"

        # 別ストアインスタンスから読み直してもディスクに残っている
        mcp_server._project_store = None
        project = mcp_server.read_project("選考2026")
        researcher = project["researchers"][0]
        assert researcher["name"] == "田中太郎"
        assert researcher["scopus"]["h_index"] == 12

    def test_save_researcher_section_merges(self, store_dir):
        mcp_server.save_researcher_section("P", "R", "scopus", {"a": 1})
        mcp_server.save_researcher_section("P", "R", "scopus", {"b": 2})
        scopus = mcp_server.read_project("P")["researchers"][0]["scopus"]
        assert scopus["a"] == 1 and scopus["b"] == 2
        # 比較時の as-of 判定に使う取得日が記録される
        assert scopus["_fetched_at"]

    def test_save_researcher_section_creates_project(self, store_dir):
        """プロジェクト未作成でも暗黙に作られる。"""
        mcp_server.save_researcher_section("新規P", "R", "ai", {"note": "x"})
        listed = mcp_server.list_projects()["projects"]
        assert [p["name"] for p in listed] == ["新規P"]
        assert listed[0]["researcher_count"] == 1

    def test_save_researcher_section_validates_input(self, store_dir):
        assert "scopus/kaken/ai" in mcp_server.save_researcher_section(
            "P", "R", "bogus", {})["error"]
        assert "object" in mcp_server.save_researcher_section(
            "P", "R", "scopus", "not-a-dict")["error"]

    def test_save_comparison(self, store_dir):
        mcp_server.create_project("P")
        mcp_server.save_comparison(
            "P", table_md="| a |", ai_evaluation="所見", selected_names=["R1", "R2"])
        comparison = mcp_server.read_project("P")["comparison"]
        assert comparison["table_md"] == "| a |"
        assert comparison["selected_names"] == ["R1", "R2"]
        assert comparison["updated_at"]

    def test_save_comparison_requires_project(self, store_dir):
        assert "not found" in mcp_server.save_comparison("nope")["error"]

    def test_mixed_fetch_dates_warn_on_read(self, store_dir):
        """比較対象の取得日がそろっていなければ警告する(拒否はしない)。"""
        import datetime
        from scopus_tools import projects as projects_mod

        mcp_server.save_researcher_section("選考", "岡村", "scopus", {"h_index": 20})
        mcp_server.save_researcher_section("選考", "田中", "scopus", {"h_index": 15})

        # 片方の取得日を 46 日前に偽装する
        store = mcp_server._get_store()
        proj = store.load("選考")
        old = (datetime.datetime.now() - datetime.timedelta(days=46)).isoformat(timespec="seconds")
        projects_mod.find_researcher(proj, "岡村")["scopus"]["_fetched_at"] = old
        store.save("選考", proj)

        result = mcp_server.read_project("選考")
        assert result["as_of_report"]["consistent"] is False
        assert "岡村" in result["as_of_warning"]
        assert "not comparable" in result["as_of_warning"]

    def test_same_day_fetches_do_not_warn(self, store_dir):
        mcp_server.save_researcher_section("選考", "A", "scopus", {"h": 1})
        mcp_server.save_researcher_section("選考", "B", "scopus", {"h": 2})
        result = mcp_server.read_project("選考")
        assert result["as_of_report"]["consistent"] is True
        assert "as_of_warning" not in result

    def test_single_researcher_never_warns(self, store_dir):
        mcp_server.save_researcher_section("P", "solo", "scopus", {"h": 1})
        assert "as_of_warning" not in mcp_server.read_project("P")

    def test_save_comparison_records_asof_report(self, store_dir):
        mcp_server.save_researcher_section("P", "A", "scopus", {"h": 1})
        mcp_server.save_researcher_section("P", "B", "scopus", {"h": 2})
        result = mcp_server.save_comparison("P", table_md="| x |")
        assert "as_of_report" in result["comparison"]

    def test_delete_project(self, store_dir):
        mcp_server.create_project("P")
        assert mcp_server.delete_project("P") == {"deleted": "P"}
        assert mcp_server.list_projects()["projects"] == []
        assert "not found" in mcp_server.delete_project("P")["error"]


# ---------------------------------------------------------------------------
# core.parse_year_range (cli から切り出した純関数)
# ---------------------------------------------------------------------------

class TestParseYearRange:
    @pytest.mark.parametrize("text", ["2021-2025", "2021,2025", "2021:2025", "[2021,2025]", " [2021, 2025] "])
    def test_accepted_forms(self, text):
        from scopus_tools.core import parse_year_range
        assert parse_year_range(text) == (2021, 2025)

    def test_none_uses_default(self):
        from scopus_tools.core import parse_year_range, default_eval_year_range
        assert parse_year_range(None) == default_eval_year_range()

    @pytest.mark.parametrize("text", ["2021", "abc-def", "2021-2022-2023", ""])
    def test_invalid_raises(self, text):
        from scopus_tools.core import parse_year_range
        with pytest.raises(ValueError):
            parse_year_range(text)

    def test_reversed_raises(self):
        from scopus_tools.core import parse_year_range
        with pytest.raises(ValueError):
            parse_year_range("2025-2021")


# ---------------------------------------------------------------------------
# ツール登録
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_exposed_tools(self):
        """公開ツールは取得系とプロジェクト永続化のみ(AI 評価は持たない)。"""
        names = {fn.__name__ for fn in mcp_server._TOOLS}
        assert names == {
            "search_author", "find_papers", "author_profile", "author_summary",
            "list_papers", "kaken_search_researcher", "kaken_grants",
            "link_kaken_researcher",
            "wos_find_document", "wos_author_documents", "journal_metrics",
            "list_projects", "read_project", "create_project",
            "delete_project", "save_researcher_section", "save_comparison",
            "cache_stats",
        }

    def test_package_has_no_llm_api_modules(self):
        """API 経由の LLM 呼び出しはパッケージから削除済み。"""
        import importlib

        for name in ("scopus_tools.ai_engine", "scopus_tools.llm", "scopus_tools.webui"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(name)

    def test_all_tools_have_docstrings(self):
        # docstring は MCP のツール説明としてモデルに渡るため必須
        assert all(fn.__doc__ for fn in mcp_server._TOOLS)


class TestScieOnlyIndependentOfOtherAnnotations:
    """`scie_only` の絞り込みは、他の注釈が有効かどうかに依存してはいけない。

    v0.11.0 で JCR の注釈を足したとき、この絞り込みが `if _JCR_TABLE:` の下に
    入れ子になってしまい、**JCR CSV を読んでいないと scie_only が黙って無視された**。
    人事選考では「SCIE 収録のみで N 件」が水増しされる形になるので、静かに壊れると危ない。
    """

    def _client(self, papers):
        from scopus_tools.api import FetchResult
        client = MagicMock()
        client._http.refresh = False
        client.search_papers_detailed.return_value = FetchResult(
            papers=papers, complete=True, request_count=1, expected_total=len(papers))
        return client

    def _papers(self):
        return [
            {"title": "in SCIE", "year": 2023, "citations": 5, "issn": "1111-1111"},
            {"title": "not indexed", "year": 2023, "citations": 1, "issn": "2222-2222"},
        ]

    def test_scie_only_filters_without_jcr_loaded(self, monkeypatch, scopus_env):
        monkeypatch.setattr(mcp_server, "_scopus_client", self._client(self._papers()))
        monkeypatch.setattr(mcp_server, "_INDEX_SETS", {"SCIE": {"11111111"}})
        monkeypatch.setattr(mcp_server, "_JCR_TABLE", {})       # JCR は無い
        result = mcp_server.list_papers("111", scie_only=True)
        assert result["total_count"] == 1
        assert result["papers"][0]["title"] == "in SCIE"

    def test_scie_only_still_filters_with_jcr_loaded(self, monkeypatch, scopus_env):
        monkeypatch.setattr(mcp_server, "_scopus_client", self._client(self._papers()))
        monkeypatch.setattr(mcp_server, "_INDEX_SETS", {"SCIE": {"11111111"}})
        monkeypatch.setattr(mcp_server, "_JCR_TABLE",
                            {"11111111": {"jif": 1.0, "quartile": "Q1", "jci": None,
                                          "jcr_year": 2025, "categories": []}})
        result = mcp_server.list_papers("111", scie_only=True)
        assert result["total_count"] == 1
