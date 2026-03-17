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
        from scopus_tools.ai_engine import estimate_expertise

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="深層学習の専門家です。"))]
        )
        with patch("scopus_tools.ai_engine.OpenAI", return_value=mock_client), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}):
            result = estimate_expertise(DUMMY_PAPERS, lang="ja")

        assert "深層学習" in result

    def test_no_api_key_returns_message(self):
        from scopus_tools.ai_engine import estimate_expertise

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            result = estimate_expertise(DUMMY_PAPERS)
        assert "OpenAI API key not found" in result


# ---------------------------------------------------------------------------
# cli.py のテスト
# ---------------------------------------------------------------------------

class TestCli:
    def test_analyze_command(self, capsys):
        from scopus_tools.cli import main

        mock_client = MagicMock()
        mock_client.search_papers.return_value = DUMMY_PAPERS

        with patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.ai_engine.estimate_expertise", return_value="AI分析結果"), \
             patch("scopus_tools.cli.load_dotenv"), \
             patch("sys.argv", ["scopus-tools", "analyze", "12345678,87654321"]):
            main()

        captured = capsys.readouterr()
        assert "AI分析結果" in captured.out
        mock_client.search_papers.assert_called_once_with(["12345678", "87654321"])
