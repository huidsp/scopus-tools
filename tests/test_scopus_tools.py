"""
ダミーデータを使ったユニットテスト
実行: python -m pytest tests/ -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_response


def _fetched(papers):
    """search_papers_detailed のダミー戻り値(完全に取れた想定)。"""
    from scopus_tools.api import FetchResult
    return FetchResult(papers=papers, complete=True, request_count=1,
                       expected_total=len(papers))

# ---------------------------------------------------------------------------
# ダミーデータ
# ---------------------------------------------------------------------------

DUMMY_PAPERS = [
    {"title": "Deep Learning for Image Recognition", "year": 2023, "citations": 150, "journal": "IEEE TPAMI", "type": "Article", "auth_list": ["Tanaka T.", "Sato K."]},
    {"title": "Graph Neural Networks in Drug Discovery", "year": 2022, "citations": 80,  "journal": "Nature Comms",  "type": "Article", "auth_list": ["Tanaka T."]},
    {"title": "Transformer-based NLP Survey",          "year": 2021, "citations": 60,  "journal": "ACL",            "type": "Review",  "auth_list": ["Tanaka T.", "Kim J."]},
    {"title": "Federated Learning Privacy",             "year": 2020, "citations": 40,  "journal": "AAAI",           "type": "Article", "auth_list": ["Tanaka T."]},
    {"title": "Quantum Computing Basics",               "year": 2019, "citations": 10,  "journal": "Phys Rev",       "type": "Article", "auth_list": ["Tanaka T."]},
    {"title": "Zero-shot Learning Approaches",          "year": 2018, "citations": 5,   "journal": "CVPR",           "type": "Article", "auth_list": ["Tanaka T."]},
]

DUMMY_CITATIONS = [c["citations"] for c in DUMMY_PAPERS]  # [150, 80, 60, 40, 10, 5]


# ---------------------------------------------------------------------------
# core.py のテスト
# ---------------------------------------------------------------------------

class TestComputeIndices:
    def test_basic(self):
        from scopus_tools.core import compute_indices
        h, g = compute_indices(DUMMY_CITATIONS)
        # citations sorted desc: 150, 80, 60, 40, 10, 5
        # h-index: 5 (5th paper has 10 >= 5? yes; 6th: 5 >= 6? no) → h=5
        assert h == 5
        # g-index: cumsum [150,230,290,330,340,345]; g² ≤ cumsum
        # g=6: 36 ≤ 345 yes; g=7: impossible (only 6 papers) → g=6
        assert g == 6

    def test_empty(self):
        from scopus_tools.core import compute_indices
        h, g = compute_indices([])
        assert h == 0
        assert g == 0

    def test_single_paper(self):
        from scopus_tools.core import compute_indices
        h, g = compute_indices([100])
        assert h == 1
        assert g == 1

    def test_all_zero_citations(self):
        from scopus_tools.core import compute_indices
        h, g = compute_indices([0, 0, 0])
        assert h == 0
        assert g == 0


class TestDefaultEvalYearRange:
    def test_previous_year_inclusive(self):
        from scopus_tools.core import default_eval_year_range
        # 2026 年中 → 前年(2025)を含む直近 5 年 = (2021, 2025)
        assert default_eval_year_range(current_year=2026) == (2021, 2025)
        # 2030 年中 → (2025, 2029)
        assert default_eval_year_range(current_year=2030) == (2025, 2029)

    def test_custom_window(self):
        from scopus_tools.core import default_eval_year_range
        # 3 年窓
        assert default_eval_year_range(default_years=3, current_year=2026) == (2023, 2025)


class TestSummarizePapers:
    def test_totals(self):
        from scopus_tools.core import summarize_papers
        result = summarize_papers(DUMMY_PAPERS)
        assert result["total_count"] == 6
        assert result["total_citations"] == sum(DUMMY_CITATIONS)
        assert result["h_index"] == 5
        assert result["g_index"] == 6

    def test_start_year(self):
        from scopus_tools.core import summarize_papers
        result = summarize_papers(DUMMY_PAPERS)
        assert result["start_year"] == 2018

    def test_recent_count(self):
        from scopus_tools.core import summarize_papers
        import datetime
        current_year = datetime.datetime.now().year
        result = summarize_papers(DUMMY_PAPERS, recent_years=5)
        expected = [p for p in DUMMY_PAPERS if p["year"] >= current_year - 4]
        assert result["recent_count"] == len(expected)

    def test_empty_papers(self):
        from scopus_tools.core import summarize_papers
        result = summarize_papers([])
        assert result["total_count"] == 0
        assert result["total_citations"] == 0
        assert result["start_year"] is None
        assert result["has_scie_data"] is False
        assert result["total_scie"] == 0

    def test_no_scie_annotation(self):
        from scopus_tools.core import summarize_papers
        result = summarize_papers(DUMMY_PAPERS)
        # DUMMY_PAPERS は is_scie を持たないため SCIE 集計は無効・0。
        assert result["has_scie_data"] is False
        assert result["total_scie"] == 0
        assert result["recent_scie"] == 0

    def test_scie_counts(self):
        from scopus_tools.core import summarize_papers
        import datetime
        cy = datetime.datetime.now().year
        papers = [
            # 評価期間内・SCIE・筆頭
            {"title": "A", "year": cy - 1, "citations": 10, "is_scie": True, "is_first_author": True},
            # 評価期間内・SCIE・非筆頭
            {"title": "B", "year": cy - 1, "citations": 5, "is_scie": True, "is_first_author": False},
            # 評価期間内・非SCIE・筆頭
            {"title": "C", "year": cy - 1, "citations": 3, "is_scie": False, "is_first_author": True},
            # 期間外・SCIE・筆頭
            {"title": "D", "year": cy - 10, "citations": 2, "is_scie": True, "is_first_author": True},
        ]
        r = summarize_papers(papers, recent_years=5)
        assert r["has_scie_data"] is True
        assert r["total_scie"] == 3
        assert r["total_scie_first_author"] == 2
        assert r["recent_scie"] == 2
        assert r["recent_scie_first_author"] == 1


# ---------------------------------------------------------------------------
# api.py のテスト (HTTP通信をモック)
# ---------------------------------------------------------------------------

class TestScopusClientInit:
    def test_raises_without_api_key(self):
        from scopus_tools.api import ScopusClient
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SCOPUS_API_KEY", None)
            with pytest.raises(ValueError, match="SCOPUS_API_KEY"):
                ScopusClient()

    def test_accepts_explicit_key(self):
        from scopus_tools.api import ScopusClient
        client = ScopusClient(api_key="dummy_key")
        assert client.api_key == "dummy_key"

    def test_reads_env_key(self):
        from scopus_tools.api import ScopusClient
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "env_key"}):
            client = ScopusClient()
            assert client.api_key == "env_key"


class TestGetAuthorProfile:
    def _make_client(self):
        from scopus_tools.api import ScopusClient
        return ScopusClient(api_key="dummy_key")

    def test_success(self):
        mock_response = make_response({
            "author-retrieval-response": [{
                "author-profile": {
                    "preferred-name": {
                        "given-name": "Taro",
                        "surname": "Tanaka"
                    }
                }
            }]
        })
        with patch("requests.get", return_value=mock_response):
            client = self._make_client()
            given, surname = client.get_author_profile("12345678")
        assert given == "Taro"
        assert surname == "Tanaka"

    def test_http_error_returns_none(self):
        mock_response = make_response(status_code=404)
        with patch("requests.get", return_value=mock_response):
            client = self._make_client()
            given, surname = client.get_author_profile("00000000")
        assert given is None
        assert surname is None

    def test_unexpected_json_returns_none(self):
        mock_response = make_response({})  # 期待するキーが存在しない
        with patch("requests.get", return_value=mock_response):
            client = self._make_client()
            given, surname = client.get_author_profile("12345678")
        assert given is None
        assert surname is None


class TestSearchPapers:
    def _make_client(self):
        from scopus_tools.api import ScopusClient
        return ScopusClient(api_key="dummy_key")

    def _make_search_response(self, entries, total=None):
        total = total if total is not None else len(entries)
        return make_response({
            "search-results": {
                "opensearch:totalResults": str(total),
                "entry": entries,
            }
        })

    def test_returns_papers(self):
        entries = [
            {"eid": "e1", "dc:title": "Paper One", "prism:coverDate": "2023-01-01",
             "citedby-count": "10", "prism:publicationName": "Journal A",
             "subtypeDescription": "Article", "author": [{"authname": "Author A"}]},
            {"eid": "e2", "dc:title": "Paper Two", "prism:coverDate": "2022-06-15",
             "citedby-count": "5", "prism:publicationName": "Journal B",
             "subtypeDescription": "Review", "author": [{"authname": "Author B"}]},
        ]
        with patch("requests.get", return_value=self._make_search_response(entries)):
            client = self._make_client()
            papers = client.search_papers(["12345678"])

        assert len(papers) == 2
        titles = {p["title"] for p in papers}
        assert "Paper One" in titles
        assert "Paper Two" in titles

    def test_deduplicates_by_eid(self):
        # 同一 eid が複数回現れてもユニークになること
        entry = {"eid": "e1", "dc:title": "Duplicate", "prism:coverDate": "2023-01-01",
                 "citedby-count": "1", "prism:publicationName": "J",
                 "subtypeDescription": "Article", "author": [{"authname": "A"}]}
        with patch("requests.get", return_value=self._make_search_response([entry, entry])):
            client = self._make_client()
            papers = client.search_papers(["111"])
        assert len(papers) == 1

    def test_http_error_returns_empty(self):
        """HTTP 500 は既定でリトライされるので、リトライ無しのレイヤで検証する。"""
        from scopus_tools.httpcache import HttpLayer
        from scopus_tools.api import ScopusClient

        mock_response = make_response(status_code=500)
        with patch("requests.get", return_value=mock_response) as get_mock:
            client = ScopusClient(api_key="dummy_key",
                                  http=HttpLayer(auth_params={"apiKey": "dummy_key"},
                                                 max_retries=0))
            papers = client.search_papers(["999"])
        assert papers == []
        assert get_mock.call_count == 1

    def test_http_error_is_retried(self):
        """5xx は一時障害なのでリトライする(現状は黙って切り詰め結果を返していた)。"""
        from scopus_tools.httpcache import HttpLayer
        from scopus_tools.api import ScopusClient

        mock_response = make_response(status_code=503)
        with patch("requests.get", return_value=mock_response) as get_mock, \
             patch("time.sleep"):
            client = ScopusClient(api_key="dummy_key",
                                  http=HttpLayer(auth_params={"apiKey": "dummy_key"},
                                                 max_retries=2))
            papers = client.search_papers(["999"])
        assert papers == []
        assert get_mock.call_count == 3      # 初回 + リトライ 2 回


# ---------------------------------------------------------------------------
# cli.py のテスト
# ---------------------------------------------------------------------------

_DUMMY_ENV = {
    "SCOPUS_API_KEY": "dummy",
    "KAKEN_APP_ID": "dummy",
}


class TestCli:
    def test_summary_years_option(self):
        from scopus_tools.cli import main

        mock_client = MagicMock()
        mock_client.search_papers_detailed.return_value = _fetched(DUMMY_PAPERS)
        mock_client.search_papers.return_value = DUMMY_PAPERS
        mock_client.get_author_profile.return_value = ("Taro", "Tanaka")

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.core.summarize_papers", return_value={
                 "has_data": True,
                 "total_count": 1,
                 "total_citations": 1,
                 "h_index": 1,
                 "g_index": 1,
                 "recent_count": 1,
                 "recent_citations": 1,
                 "total_first_author": 0,
                 "recent_first_author": 0,
                 "research_years": 1,
                 "start_year": 2026,
             }) as summarize_mock, \
             patch("scopus_tools.utils.print_report_text") as print_mock, \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", ["scopus-tools", "summary", "12345678", "--years", "[2021,2025]"]):
            main()

        summarize_mock.assert_called_once_with(DUMMY_PAPERS, year_range=(2021, 2025))
        print_mock.assert_called_once()
        assert print_mock.call_args.kwargs["year_range"] == (2021, 2025)

    def test_batch_years_option(self):
        from scopus_tools.cli import main

        mock_client = MagicMock()

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.utils.process_batch_summary") as batch_mock, \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", [
                 "scopus-tools",
                 "batch",
                 "--input",
                 "in.csv",
                 "--output",
                 "out.csv",
                 "--years",
                 "[2021,2025]",
             ]):
            main()

        batch_mock.assert_called_once_with("in.csv", "out.csv", mock_client, year_range=(2021, 2025))

    def test_missing_api_key_errors_before_running(self, capsys):
        from scopus_tools.cli import main

        with patch.dict(os.environ, {}, clear=True), \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", ["scopus-tools", "summary", "12345678"]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        assert "SCOPUS_API_KEY" in captured.err

    def test_batch_accepts_dashed_year_range(self):
        from scopus_tools.cli import main

        mock_client = MagicMock()

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.utils.process_batch_summary") as batch_mock, \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", [
                 "scopus-tools", "batch",
                 "--input", "in.csv", "--output", "out.csv",
                 "--years", "2021-2025",
             ]):
            main()

        batch_mock.assert_called_once_with("in.csv", "out.csv", mock_client, year_range=(2021, 2025))

    def test_mcp_subcommand_passes_dirs_through(self, tmp_path):
        """mcp サブコマンドは鍵が無くても起動でき、各ディレクトリを run に渡す。"""
        from scopus_tools.cli import main

        captured_kwargs = {}

        def fake_run(**kwargs):
            captured_kwargs.update(kwargs)

        with patch.dict(os.environ, {}, clear=True), \
             patch("scopus_tools.cli._load_env_files"), \
             patch("scopus_tools.mcp_server.run", side_effect=fake_run), \
             patch("sys.argv", [
                 "scopus-tools", "mcp",
                 "--projects-dir", str(tmp_path / "projects"),
                 "--scie-dir", str(tmp_path / "index"),
             ]):
            main()

        assert captured_kwargs["projects_dir"] == str(tmp_path / "projects")
        assert captured_kwargs["scie_dir"] == str(tmp_path / "index")
        assert captured_kwargs["scie_list"] is None

    def test_search_rejects_mixed_modes(self, capsys):
        from scopus_tools.cli import main

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", [
                 "scopus-tools", "search",
                 "--name", "Foo Bar",
                 "--input", "in.csv", "--output", "out.csv",
             ]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        assert "--name" in captured.err and "--input" in captured.err

    def test_summary_json_format_emits_json(self, capsys):
        import json
        from scopus_tools.cli import main

        mock_client = MagicMock()
        mock_client.search_papers_detailed.return_value = _fetched(DUMMY_PAPERS)
        mock_client.search_papers.return_value = DUMMY_PAPERS
        mock_client.get_author_profile.return_value = ("Taro", "Tanaka")

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", [
                 "scopus-tools", "summary", "12345678",
                 "--years", "2021-2025", "--format", "json",
             ]):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["scopus_ids"] == ["12345678"]
        assert data["author"] == {"first": "Taro", "last": "Tanaka"}
        assert data["year_range"] == [2021, 2025]
        assert data["report"]["total_count"] == len(DUMMY_PAPERS)
        assert len(data["papers"]) == len(DUMMY_PAPERS)

    def test_summary_input_mode_iterates_csv_rows(self, tmp_path):
        from scopus_tools import utils
        from scopus_tools.cli import main

        csv_path = tmp_path / "in.csv"
        utils.save_output_csv([
            {"Name": "A", "Scopus ID": "100"},
            {"Name": "B", "Scopus ID": "200,201"},
        ], str(csv_path))

        mock_client = MagicMock()
        mock_client.search_papers_detailed.return_value = _fetched(DUMMY_PAPERS)
        mock_client.search_papers.return_value = DUMMY_PAPERS
        mock_client.get_author_profile.return_value = ("Taro", "Tanaka")

        out_path = tmp_path / "out.json"
        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", [
                 "scopus-tools", "summary",
                 "--input", str(csv_path),
                 "--format", "json",
                 "--output", str(out_path),
                 "--years", "2021-2025",
             ]):
            main()

        import json
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert isinstance(data, list) and len(data) == 2
        assert data[0]["scopus_ids"] == ["100"]
        assert data[1]["scopus_ids"] == ["200", "201"]

    def test_summary_rejects_missing_ids_and_input(self, capsys):
        from scopus_tools.cli import main

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.cli._load_env_files"), \
             patch("sys.argv", ["scopus-tools", "summary"]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        assert "Scopus IDs" in captured.err or "--input" in captured.err


# ---------------------------------------------------------------------------
# CSV I/O (pandas を外した後の非退行)
# ---------------------------------------------------------------------------

class TestCsvIO:
    """pandas を標準ライブラリの csv に置き換えたときの出力互換性を固定する。"""

    def test_output_uses_utf8_sig_and_lf(self, tmp_path):
        """Excel 用の BOM と、pandas 時代と同じ LF 改行を維持する
        (csv モジュールの既定は CRLF なので明示が要る)。"""
        from scopus_tools import utils

        path = tmp_path / "out.csv"
        utils.save_output_csv([{"Name": "岡村", "ID": "1"}], str(path))
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")     # UTF-8 BOM
        assert b"\r\n" not in raw                  # CRLF ではない
        assert raw.endswith(b"\n")

    def test_roundtrip(self, tmp_path):
        from scopus_tools import utils

        rows = [{"Name": "A", "Scopus ID": "100,101"}, {"Name": "B", "Scopus ID": ""}]
        path = tmp_path / "rt.csv"
        utils.save_output_csv(rows, str(path))
        back = utils.read_input_csv(str(path))
        assert list(back) == rows
        assert back.columns == ["Name", "Scopus ID"]

    def test_columns_are_union_of_all_row_keys(self, tmp_path):
        """pandas の DataFrame(list) は全行のキーの和集合を列にしていた。"""
        from scopus_tools import utils

        path = tmp_path / "u.csv"
        utils.save_output_csv([{"a": 1}, {"b": 2}], str(path))
        back = utils.read_input_csv(str(path))
        assert back.columns == ["a", "b"]
        assert back[0] == {"a": "1", "b": ""}

    def test_empty_cells_are_empty_strings_not_nan(self, tmp_path):
        """pandas 時代は NaN になっていた。CLI 側の空 ID 判定がこれに依存する。"""
        from scopus_tools import utils

        path = tmp_path / "e.csv"
        path.write_text("Name,Scopus ID\nA,\n", encoding="utf-8")
        rows = utils.read_input_csv(str(path))
        assert rows[0]["Scopus ID"] == ""

    def test_required_cols_error_names_missing_and_found(self, tmp_path):
        from scopus_tools import utils

        path = tmp_path / "m.csv"
        path.write_text("Name\nA\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Scopus ID"):
            utils.read_input_csv(str(path), required_cols=["Scopus ID"])

    def test_bom_input_is_read_without_bom_in_column_name(self, tmp_path):
        """自分で書いた utf-8-sig の CSV を読み直しても列名に BOM が残らない。"""
        from scopus_tools import utils

        path = tmp_path / "b.csv"
        utils.save_output_csv([{"Name": "A"}], str(path))
        assert utils.read_input_csv(str(path)).columns == ["Name"]
