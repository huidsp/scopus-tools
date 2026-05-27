"""
ダミーデータを使ったユニットテスト
実行: python -m pytest tests/ -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "author-retrieval-response": [{
                "author-profile": {
                    "preferred-name": {
                        "given-name": "Taro",
                        "surname": "Tanaka"
                    }
                }
            }]
        }
        with patch("requests.get", return_value=mock_response):
            client = self._make_client()
            given, surname = client.get_author_profile("12345678")
        assert given == "Taro"
        assert surname == "Tanaka"

    def test_http_error_returns_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 404
        with patch("requests.get", return_value=mock_response):
            client = self._make_client()
            given, surname = client.get_author_profile("00000000")
        assert given is None
        assert surname is None

    def test_unexpected_json_returns_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # 期待するキーが存在しない
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
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "search-results": {
                "opensearch:totalResults": str(total),
                "entry": entries,
            }
        }
        return mock_response

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
        mock_response = MagicMock()
        mock_response.status_code = 500
        with patch("requests.get", return_value=mock_response):
            client = self._make_client()
            papers = client.search_papers(["999"])
        assert papers == []


# ---------------------------------------------------------------------------
# ai_engine.py のテスト (OpenAI をモック)
# ---------------------------------------------------------------------------

class TestEstimateExpertise:
    def test_returns_analysis(self):
        from scopus_tools import llm
        from scopus_tools.ai_engine import estimate_expertise

        default_key = llm.required_key_for(llm.DEFAULT_MODEL)
        with patch.dict(os.environ, {default_key: "dummy"}, clear=True), \
             patch("scopus_tools.llm.complete", return_value="深層学習の専門家です。") as mock_complete:
            result = estimate_expertise(DUMMY_PAPERS, lang="ja")

        assert "深層学習" in result
        # 既定モデルで呼ばれる
        assert mock_complete.call_args.args[0] == llm.DEFAULT_MODEL

    def test_no_api_key_returns_message(self):
        from scopus_tools import llm
        from scopus_tools.ai_engine import estimate_expertise

        default_key = llm.required_key_for(llm.DEFAULT_MODEL)
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(default_key, None)
            result = estimate_expertise(DUMMY_PAPERS)
        assert default_key in result and "Skipping" in result

    def test_explicit_model_routes_to_matching_provider(self):
        """OpenAI/Claude どちらの明示指定でも、その鍵だけ参照する。"""
        from scopus_tools.ai_engine import estimate_expertise

        with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}, clear=True), \
             patch("scopus_tools.llm.complete", return_value="OpenAI") as mock_complete:
            result = estimate_expertise(DUMMY_PAPERS, lang="ja", model="gpt-5.4")
        assert "OpenAI" in result
        assert mock_complete.call_args.args[0] == "gpt-5.4"

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy"}, clear=True), \
             patch("scopus_tools.llm.complete", return_value="Claude") as mock_complete:
            result = estimate_expertise(DUMMY_PAPERS, lang="ja", model="claude-opus-4-7")
        assert "Claude" in result
        assert mock_complete.call_args.args[0] == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# cli.py のテスト
# ---------------------------------------------------------------------------

_DUMMY_ENV = {
    "SCOPUS_API_KEY": "dummy",
    "OPENAI_API_KEY": "dummy",
    "ANTHROPIC_API_KEY": "dummy",
    "KAKEN_APP_ID": "dummy",
}


class TestCli:
    def test_analyze_command(self, capsys):
        from scopus_tools.cli import main

        mock_client = MagicMock()
        mock_client.search_papers.return_value = DUMMY_PAPERS

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.ai_engine.estimate_expertise", return_value="AI分析結果"), \
             patch("scopus_tools.cli.load_dotenv"), \
             patch("sys.argv", ["scopus-tools", "analyze", "12345678,87654321"]):
            main()

        captured = capsys.readouterr()
        assert "AI分析結果" in captured.out
        mock_client.search_papers.assert_called_once_with(["12345678", "87654321"])

    def test_summary_years_option(self):
        from scopus_tools.cli import main

        mock_client = MagicMock()
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
             patch("scopus_tools.cli.load_dotenv"), \
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
             patch("scopus_tools.cli.load_dotenv"), \
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
             patch("scopus_tools.cli.load_dotenv"), \
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
             patch("scopus_tools.cli.load_dotenv"), \
             patch("sys.argv", [
                 "scopus-tools", "batch",
                 "--input", "in.csv", "--output", "out.csv",
                 "--years", "2021-2025",
             ]):
            main()

        batch_mock.assert_called_once_with("in.csv", "out.csv", mock_client, year_range=(2021, 2025))

    def test_webui_subcommand_accepts_projects_dir(self, capsys, tmp_path):
        from scopus_tools.cli import main

        captured_kwargs = {}

        def fake_launch(**kwargs):
            captured_kwargs.update(kwargs)

        with patch.dict(os.environ, {}, clear=True), \
             patch("scopus_tools.cli.load_dotenv"), \
             patch("scopus_tools.webui.launch", side_effect=fake_launch), \
             patch("sys.argv", [
                 "scopus-tools", "webui",
                 "--port", "9999",
                 "--projects-dir", str(tmp_path / "projects"),
             ]):
            main()

        assert captured_kwargs["projects_dir"] == str(tmp_path / "projects")
        assert captured_kwargs["port"] == 9999
        assert captured_kwargs["host"] == "127.0.0.1"

    def test_search_rejects_mixed_modes(self, capsys):
        from scopus_tools.cli import main

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.cli.load_dotenv"), \
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
        mock_client.search_papers.return_value = DUMMY_PAPERS
        mock_client.get_author_profile.return_value = ("Taro", "Tanaka")

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.cli.load_dotenv"), \
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
        import pandas as pd
        from scopus_tools.cli import main

        csv_path = tmp_path / "in.csv"
        pd.DataFrame([
            {"Name": "A", "Scopus ID": "100"},
            {"Name": "B", "Scopus ID": "200,201"},
        ]).to_csv(csv_path, index=False)

        mock_client = MagicMock()
        mock_client.search_papers.return_value = DUMMY_PAPERS
        mock_client.get_author_profile.return_value = ("Taro", "Tanaka")

        out_path = tmp_path / "out.json"
        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.cli.load_dotenv"), \
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
             patch("scopus_tools.cli.load_dotenv"), \
             patch("sys.argv", ["scopus-tools", "summary"]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        assert "Scopus IDs" in captured.err or "--input" in captured.err

    def test_eval_auto_links_kaken_by_name(self, capsys):
        from scopus_tools.cli import main

        scopus_mock = MagicMock()
        scopus_mock.search_papers.return_value = DUMMY_PAPERS
        scopus_mock.get_author_profile.return_value = ("Taro", "Tanaka")

        kaken_mock = MagicMock()
        kaken_mock.search_researcher_by_name.return_value = [
            {"researcher_id": "80401243", "name": "田中 太郎", "affiliation": "Foo Univ"},
        ]
        kaken_mock.get_grants_by_researcher_id.return_value = [
            {"title": "G1", "my_role": "principal_investigator"},
        ]

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=scopus_mock), \
             patch("scopus_tools.kaken.KakenClient", return_value=kaken_mock), \
             patch("scopus_tools.ai_engine.evaluate_achievements", return_value="評価本文"), \
             patch("scopus_tools.cli.load_dotenv"), \
             patch("sys.argv", [
                 "scopus-tools", "eval", "12345678",
                 "--years", "2021-2025",
             ]):
            main()

        kaken_mock.search_researcher_by_name.assert_called_once()
        kaken_mock.get_grants_by_researcher_id.assert_called_once_with("80401243", lang="ja")
        # evaluate_achievements should have been passed the grants list
        from scopus_tools import ai_engine  # noqa: F401
        captured = capsys.readouterr()
        assert "KAKEN研究者番号: 80401243" in captured.out
        assert "評価本文" in captured.out

    def test_eval_no_kaken_skips_auto_link(self, capsys):
        from scopus_tools.cli import main

        scopus_mock = MagicMock()
        scopus_mock.search_papers.return_value = DUMMY_PAPERS
        scopus_mock.get_author_profile.return_value = ("Taro", "Tanaka")

        kaken_mock = MagicMock()

        with patch.dict(os.environ, _DUMMY_ENV), \
             patch("scopus_tools.api.ScopusClient", return_value=scopus_mock), \
             patch("scopus_tools.kaken.KakenClient", return_value=kaken_mock), \
             patch("scopus_tools.ai_engine.evaluate_achievements", return_value="評価本文"), \
             patch("scopus_tools.cli.load_dotenv"), \
             patch("sys.argv", [
                 "scopus-tools", "eval", "12345678",
                 "--years", "2021-2025", "--no-kaken",
             ]):
            main()

        kaken_mock.search_researcher_by_name.assert_not_called()
        kaken_mock.get_grants_by_researcher_id.assert_not_called()
        captured = capsys.readouterr()
        assert "KAKEN研究者番号" not in captured.out
