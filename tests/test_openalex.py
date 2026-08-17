"""OpenAlex クライアントと、その過剰マージ対策のテスト。

ネットワークには触らない。`_get` をモックするか、パース関数を直接呼ぶ。
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from scopus_tools import mcp_server, openalex
from scopus_tools.openalex import (OpenAlexClient, merge_risk, normalize_author_id,
                                   normalize_ror, parse_author, parse_work)


def _work(**over):
    w = {
        "id": "https://openalex.org/W123",
        "display_name": "A paper about reliability",
        "publication_year": 2020,
        "cited_by_count": 7,
        "doi": "https://doi.org/10.1000/abc",
        "type": "article",
        "open_access": {"is_oa": True},
        "biblio": {"volume": "12", "issue": "3",
                   "first_page": "100", "last_page": "110"},
        "primary_location": {"source": {
            "display_name": "Journal of Testing",
            "issn_l": "1234-5678", "issn": ["1234-5678", "8765-4321"],
            "type": "journal"}},
        "authorships": [
            {"author": {"id": "https://openalex.org/A1", "display_name": "First Author",
                        "orcid": "https://orcid.org/0000-0000-0000-0001"},
             "institutions": [{"display_name": "Some University"}]},
            {"author": {"id": "https://openalex.org/A2", "display_name": "Second Author"},
             "institutions": []},
        ],
    }
    w.update(over)
    return w


class TestNormalizers:

    @pytest.mark.parametrize("raw,expected", [
        ("A5000000001", "A5000000001"),
        ("https://openalex.org/A5000000001", "A5000000001"),
        ("https://openalex.org/A5000000001/", "A5000000001"),
        ("a5000000001", "A5000000001"),
        (None, ""),
    ])
    def test_author_id(self, raw, expected):
        assert normalize_author_id(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("01abcd234", "https://ror.org/01abcd234"),
        ("https://ror.org/01abcd234", "https://ror.org/01abcd234"),
        ("https://ror.org/01abcd234/", "https://ror.org/01abcd234"),
    ])
    def test_ror(self, raw, expected):
        assert normalize_ror(raw) == expected


class TestParseWork:
    """Scopus 側 `api.parse_entry` と同じ形にする(core / scie をそのまま通すため)。"""

    def test_shape_matches_scopus(self):
        from scopus_tools.api import parse_entry
        oa = set(parse_work(_work()))
        sc = set(parse_entry({"dc:title": "t"}))
        missing = sc - oa
        assert not missing, f"OpenAlex paper dict is missing Scopus keys: {missing}"

    def test_core_fields(self):
        p = parse_work(_work())
        assert p["title"] == "A paper about reliability"
        assert p["year"] == 2020
        assert p["citations"] == 7
        assert p["doi"] == "10.1000/abc"          # https://doi.org/ は剥がす
        assert p["journal"] == "Journal of Testing"
        assert p["issn"] == "1234-5678"
        assert p["pages"] == "100-110"
        assert p["open_access"] is True
        assert p["source"] == "openalex"

    def test_author_position_unknown_without_ids(self):
        """誰の論文か分からないときは False/0 ではなく None(Scopus 側と同じ規約)。"""
        p = parse_work(_work())
        assert p["is_first_author"] is None
        assert p["author_position"] is None

    def test_author_position_with_ids(self):
        p = parse_work(_work(), author_ids=["A2"])
        assert p["is_first_author"] is False
        assert p["author_position"] == 2
        assert p["author_count"] == 2

    def test_first_author(self):
        p = parse_work(_work(), author_ids=["https://openalex.org/A1"])
        assert p["is_first_author"] is True
        assert p["author_position"] == 1

    def test_missing_year_is_zero_not_crash(self):
        assert parse_work(_work(publication_year=None))["year"] == 0

    def test_abstract_is_opt_in_and_reconstructed(self):
        w = _work(abstract_inverted_index={"Software": [0], "reliability": [1],
                                           "matters": [2]})
        assert "abstract" not in parse_work(w)
        assert parse_work(w, include_abstract=True)["abstract"] == \
            "Software reliability matters"

    def test_no_source_does_not_crash(self):
        p = parse_work(_work(primary_location=None))
        assert p["journal"] == ""
        assert p["issn"] == ""


class TestMergeRisk:
    """OpenAlex は同姓同名を強くマージする。過大評価は人事選考で最も危険。"""

    def test_many_institutions_is_high_risk(self):
        af = [{"name": f"Univ {i}", "years": [2000 + i]} for i in range(5)]
        assert merge_risk(af)["level"] == "high"

    def test_long_span_is_high_risk(self):
        af = [{"name": "A", "years": [1960]}, {"name": "B", "years": [2020]}]
        assert merge_risk(af)["level"] == "high"
        assert merge_risk(af)["year_span"] == 60

    def test_single_institution_is_low_risk(self):
        af = [{"name": "Hiroshima University", "years": [2020, 2021, 2022]}]
        assert merge_risk(af)["level"] == "low"

    def test_note_warns_against_using_raw_counts(self):
        note = merge_risk([{"name": "A", "years": [1960]},
                           {"name": "B", "years": [2020]}])["note"]
        assert "works_count" in note and "h_index" in note

    def test_parse_author_carries_the_risk(self):
        item = {"id": "https://openalex.org/A1", "display_name": "X",
                "works_count": 441, "cited_by_count": 3292,
                "summary_stats": {"h_index": 28, "i10_index": 95},
                "affiliations": [
                    {"institution": {"display_name": f"I{i}", "ror": ""},
                     "years": [1970 + i * 10]} for i in range(5)]}
        a = parse_author(item)
        assert a["h_index"] == 28
        assert a["merge_risk"]["level"] == "high"


class TestAuthorWorksRequiresNarrowing:
    """裸の author.id は他人の業績を含みうるので、機械的に禁じる。"""

    def _client(self):
        c = OpenAlexClient(http=MagicMock())
        c._get = MagicMock(return_value={"results": [], "meta": {"count": 0}})
        return c

    def test_unfiltered_call_is_refused(self):
        with pytest.raises(ValueError, match="institution_ror and/or year_range"):
            self._client().author_works("A5000000001")

    def test_institution_is_enough(self):
        c = self._client()
        c.author_works("A123", institution_ror="01abcd234")
        assert "institutions.ror:https://ror.org/01abcd234" in c._get.call_args[0][1]["filter"]

    def test_year_range_is_enough(self):
        c = self._client()
        c.author_works("A123", year_range=(2021, 2025))
        assert "publication_year:2021-2025" in c._get.call_args[0][1]["filter"]

    def test_missing_author_id_is_refused(self):
        with pytest.raises(ValueError, match="author_id is required"):
            self._client().author_works("", institution_ror="01abcd234")


class TestFindPaper:

    def _client(self, payload):
        c = OpenAlexClient(http=MagicMock())
        c._get = MagicMock(return_value=payload)
        return c

    def test_requires_a_criterion(self):
        with pytest.raises(ValueError, match="doi / title"):
            self._client({}).find_paper()

    def test_doi_is_normalized_into_the_filter(self):
        c = self._client({"results": [], "meta": {"count": 0}})
        c.find_paper(doi="https://doi.org/10.1000/abc")
        assert c._get.call_args[0][1]["filter"] == "doi:https://doi.org/10.1000/abc"

    def test_failed_request_is_marked_incomplete(self):
        c = self._client(None)
        assert self._client(None).find_paper(doi="x")["complete"] is False


class TestMailtoNeverReachesTheCache:
    """polite pool の連絡先は個人情報。キャッシュキーにも DB にも入れない。"""

    def test_mailto_is_a_denied_param_name(self):
        from scopus_tools.httpcache import SECRET_PARAM_NAMES, canonical_params
        assert "mailto" in SECRET_PARAM_NAMES
        assert "mailto" not in canonical_params({"mailto": "a@b.jp", "filter": "x"})

    def test_client_puts_mailto_in_auth_params(self):
        c = OpenAlexClient(mailto="a@b.jp")
        assert c._http.auth_params == {"mailto": "a@b.jp"}

    def test_no_mailto_is_allowed(self):
        assert OpenAlexClient()._http.auth_params == {}


class TestMcpTools:

    def test_author_works_refusal_becomes_an_error_dict(self, monkeypatch):
        client = MagicMock()
        client._http.refresh = False
        client.author_works.side_effect = ValueError("needs narrowing")
        monkeypatch.setattr(mcp_server, "_openalex_client", client)
        result = mcp_server.openalex_author_works("A1")
        assert "error" in result

    def test_search_author_warns_on_merged_profiles(self, monkeypatch):
        client = MagicMock()
        client._http.refresh = False
        client.search_author.return_value = [
            {"author_id": "A1", "works_count": 441,
             "merge_risk": {"level": "high"}}]
        monkeypatch.setattr(mcp_server, "_openalex_client", client)
        result = mcp_server.openalex_search_author("X")
        assert "warning" in result
        assert "works_count" in result["warning"]

    def test_no_warning_for_clean_profiles(self, monkeypatch):
        client = MagicMock()
        client._http.refresh = False
        client.search_author.return_value = [
            {"author_id": "A1", "works_count": 20, "merge_risk": {"level": "low"}}]
        monkeypatch.setattr(mcp_server, "_openalex_client", client)
        assert "warning" not in mcp_server.openalex_search_author("X")

    def test_openalex_needs_no_api_key(self, monkeypatch):
        """Scopus が 401 になる環境(学外 IP)でも動くことが導入理由の 1 つ。"""
        client = MagicMock()
        client._http.refresh = False
        client.find_paper.return_value = {"papers": [], "total_count": 0,
                                          "complete": True, "reason": None}
        monkeypatch.setattr(mcp_server, "_openalex_client", client)
        with patch.dict(os.environ, {}, clear=True):
            result = mcp_server.openalex_find_paper(doi="10.1000/abc")
        assert "error" not in result
