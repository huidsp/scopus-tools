from unittest.mock import MagicMock, patch

import pandas as pd


def make_response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class TestLegacyAuthorSearchCompatibility:
    def test_search_author_by_name_tries_both_name_orders_and_deduplicates(self):
        from scopus_tools.api import ScopusClient

        first_pattern_payload = {
            "search-results": {
                "entry": [
                    {
                        "dc:identifier": "AUTHOR_ID:100",
                        "preferred-name": {"surname": "Okamura", "given-name": "Hiroyuki"},
                        "affiliation-current": {"affiliation-name": "A University"},
                        "document-count": "12",
                    }
                ]
            }
        }
        second_pattern_payload = {
            "search-results": {
                "entry": [
                    {
                        "dc:identifier": "AUTHOR_ID:100",
                        "preferred-name": {"surname": "Okamura", "given-name": "Hiroyuki"},
                        "affiliation-current": {"affiliation-name": "A University"},
                        "document-count": "12",
                    },
                    {
                        "dc:identifier": "AUTHOR_ID:200",
                        "preferred-name": {"surname": "Okamura", "given-name": "H."},
                        "affiliation-current": {"affiliation-name": "B Institute"},
                        "document-count": "7",
                    },
                ]
            }
        }

        with patch(
            "requests.get",
            side_effect=[make_response(first_pattern_payload), make_response(second_pattern_payload)],
        ) as get_mock:
            client = ScopusClient(api_key="dummy")
            results = client.search_author_by_name("Hiroyuki Okamura")

        assert len(results) == 2
        assert [item["id"] for item in results] == ["100", "200"]
        first_query = get_mock.call_args_list[0].kwargs["params"]["query"]
        second_query = get_mock.call_args_list[1].kwargs["params"]["query"]
        assert first_query == "AUTHLASTNAME(Okamura) AND AUTHFIRST(Hiroyuki)"
        assert second_query == "AUTHLASTNAME(Hiroyuki) AND AUTHFIRST(Okamura)"


class TestLegacyPaperSearchCompatibility:
    def test_search_papers_preserves_bibliographic_fields_and_first_author_flag(self):
        from scopus_tools.api import ScopusClient

        payload = {
            "search-results": {
                "opensearch:totalResults": "2",
                "entry": [
                    {
                        "eid": "e1",
                        "dc:title": "Paper One",
                        "prism:coverDate": "2024-01-01",
                        "citedby-count": "3",
                        "prism:publicationName": "Journal A",
                        "prism:volume": "10",
                        "prism:issueIdentifier": "2",
                        "prism:pageRange": "11-20",
                        "prism:aggregationType": "Journal",
                        "subtypeDescription": "Article",
                        "author": [
                            {"authid": "123", "authname": "Okamura H."},
                            {"authid": "999", "authname": "Tanaka T."},
                        ],
                    },
                    {
                        "eid": "e1",
                        "dc:title": "Paper One",
                        "prism:coverDate": "2024-01-01",
                        "citedby-count": "8",
                        "prism:publicationName": "Journal A",
                        "subtypeDescription": "Article",
                        "author": [
                            {"authid": "999", "authname": "Tanaka T."},
                            {"authid": "123", "authname": "Okamura H."},
                        ],
                    },
                ],
            }
        }

        with patch("requests.get", return_value=make_response(payload)):
            client = ScopusClient(api_key="dummy")
            papers = client.search_papers(["123"])

        assert len(papers) == 1
        paper = papers[0]
        assert paper["volume"] == "10"
        assert paper["issue"] == "2"
        assert paper["pages"] == "11-20"
        assert paper["aggregation_type"] == "Journal"
        assert paper["authors"] == "Okamura H., Tanaka T."
        assert paper["citations"] == 8
        assert paper["is_first_author"] is True

    def test_get_papers_by_year_returns_type_breakdown(self):
        from scopus_tools.api import ScopusClient

        client = ScopusClient(api_key="dummy")
        with patch.object(
            client,
            "search_papers",
            return_value=[
                {"citations": 5, "type": "Article"},
                {"citations": 2, "type": "Review"},
                {"citations": 1, "type": None},
            ],
        ):
            result = client.get_papers_by_year(["123"], 2020, 2024)

        assert result == {
            "paper_count": 3,
            "total_citations": 8,
            "Article": 1,
            "Review": 1,
            "Unknown": 1,
        }


class TestLegacySummaryCompatibility:
    def test_summarize_papers_counts_first_author_metrics(self):
        from scopus_tools.core import summarize_papers

        papers = [
            {"year": 2024, "citations": 10, "is_first_author": True},
            {"year": 2023, "citations": 4, "is_first_author": False},
            {"year": 2019, "citations": 1, "is_first_author": True},
        ]

        result = summarize_papers(papers, recent_years=5)

        assert result["has_data"] is True
        assert result["total_first_author"] == 2
        assert result["recent_first_author"] == 1
        assert result["research_years"] >= 1


class TestLegacyUtilsCompatibility:
    def test_process_author_csv_groups_ids_by_affiliation(self, tmp_path):
        from scopus_tools import utils

        input_path = tmp_path / "authors.csv"
        output_path = tmp_path / "authors_out.csv"
        pd.DataFrame([{"Name": "Hiroyuki Okamura"}]).to_csv(input_path, index=False)

        client = MagicMock()
        client.search_author_by_name.return_value = [
            {"id": "100", "name": "Okamura Hiroyuki", "affiliation": "A University", "doc_count": "10"},
            {"id": "101", "name": "Okamura Hiroyuki", "affiliation": "A University", "doc_count": "11"},
            {"id": "200", "name": "Okamura Hiroyuki", "affiliation": "B Institute", "doc_count": "12"},
        ]

        utils.process_author_csv(str(input_path), str(output_path), client)

        result = pd.read_csv(output_path)
        assert len(result) == 2
        assert set(result["Affiliation"]) == {"A University", "B Institute"}
        a_ids = result.loc[result["Affiliation"] == "A University", "Scopus ID"].iloc[0]
        assert a_ids == "100,101"

    def test_print_report_text_includes_legacy_summary_fields(self, capsys):
        from scopus_tools import utils

        report = {
            "start_year": 2020,
            "research_years": 5,
            "total_count": 3,
            "total_citations": 25,
            "total_first_author": 2,
            "recent_count": 2,
            "recent_citations": 24,
            "recent_first_author": 1,
            "h_index": 2,
            "g_index": 3,
        }
        papers = [
            {
                "title": "Paper One",
                "authors": "Okamura H., Tanaka T.",
                "journal": "Journal A",
                "aggregation_type": "Journal",
                "volume": "10",
                "issue": "2",
                "pages": "11-20",
                "year": 2024,
                "citations": 8,
                "is_first_author": True,
                "eid": "e1",
            }
        ]

        utils.print_report_text("Hiroyuki", "Okamura", ["123", "456"], report, papers)

        output = capsys.readouterr().out
        assert "Scopus IDs: 123, 456" in output
        assert "研究歴: 2020年" in output
        assert "筆頭著者論文数" in output
        assert "Vol.10" in output
        assert "EID       : e1" in output

    def test_process_batch_summary_writes_legacy_columns(self, tmp_path):
        from scopus_tools import utils

        input_path = tmp_path / "batch.csv"
        output_path = tmp_path / "batch_out.csv"
        pd.DataFrame(
            [
                {"Name": "A", "Scopus ID": "100,101", "Affiliation": "X Univ"},
                {"Name": "B", "Scopus ID": None, "Affiliation": "Y Univ"},
            ]
        ).to_csv(input_path, index=False)

        client = MagicMock()
        client.get_author_profile.return_value = ("Hiroyuki", "Okamura")
        client.search_papers.return_value = [
            {"year": 2024, "citations": 8, "is_first_author": True},
            {"year": 2022, "citations": 3, "is_first_author": False},
        ]

        utils.process_batch_summary(str(input_path), str(output_path), client)

        result = pd.read_csv(output_path)
        assert list(result.columns) == [
            "Name",
            "Scopus IDs",
            "Affiliation",
            "Research Years",
            "Start Year",
            "Total Papers",
            "Total Citations",
            "Total First Author",
            "Recent 5Y Papers",
            "Recent 5Y Citations",
            "Recent 5Y First Author",
            "H-index",
            "G-index",
        ]
        assert len(result) == 1
        assert result.loc[0, "Scopus IDs"] == "100, 101"


class TestLegacyCliCompatibility:
    def test_stats_command_skips_missing_scopus_id_rows(self):
        from scopus_tools.cli import main

        data_frame = pd.DataFrame(
            [
                {"Name": "A", "Scopus ID": "100,101"},
                {"Name": "B", "Scopus ID": None},
            ]
        )
        mock_client = MagicMock()
        mock_client.get_papers_by_year.return_value = {"paper_count": 2, "total_citations": 9, "Article": 2}

        with patch("scopus_tools.api.ScopusClient", return_value=mock_client), \
             patch("scopus_tools.cli.load_dotenv"), \
             patch("scopus_tools.utils.read_input_csv", return_value=data_frame), \
             patch("scopus_tools.utils.save_output_csv") as save_mock, \
             patch("sys.argv", ["scopus-tools", "stats", "--year", "[2020,2024]", "--input", "in.csv", "--output", "out.csv"]):
            main()

        mock_client.get_papers_by_year.assert_called_once_with(["100", "101"], 2020, 2024)
        saved_rows = save_mock.call_args.args[0]
        assert len(saved_rows) == 1
        assert saved_rows[0]["Name"] == "A"