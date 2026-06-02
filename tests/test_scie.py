"""scie モジュール(SCIE 収録判定)のユニットテスト。"""
import pytest

from scopus_tools import scie


class TestNormalizeIssn:
    def test_strips_hyphen_and_space(self):
        assert scie.normalize_issn("0028-0836") == "00280836"
        assert scie.normalize_issn("0028 0836") == "00280836"
        assert scie.normalize_issn("00280836") == "00280836"

    def test_check_digit_x_uppercased(self):
        assert scie.normalize_issn("2049-363x") == "2049363X"

    def test_invalid_length_returns_none(self):
        assert scie.normalize_issn("123") is None
        assert scie.normalize_issn("") is None
        assert scie.normalize_issn(None) is None


class TestLoadScieIssnSet:
    def test_csv_with_issn_columns(self, tmp_path):
        f = tmp_path / "scie.csv"
        f.write_text(
            "Journal title,ISSN,eISSN\n"
            "Nature,0028-0836,1476-4687\n"
            "Science,0036-8075,1095-9203\n",
            encoding="utf-8",
        )
        issns = scie.load_scie_issn_set(str(f))
        assert "00280836" in issns
        assert "14764687" in issns
        assert "00368075" in issns
        assert len(issns) == 4

    def test_plain_text_one_per_line(self, tmp_path):
        f = tmp_path / "scie.txt"
        f.write_text("0028-0836\n1476-4687\n", encoding="utf-8")
        issns = scie.load_scie_issn_set(str(f))
        assert issns == {"00280836", "14764687"}


class TestAnnotatePapers:
    def test_matches_issn_and_eissn(self):
        issn_set = {"00280836", "14764687"}
        papers = [
            {"title": "A", "issn": "0028-0836", "eissn": ""},      # ISSN 一致
            {"title": "B", "issn": "", "eissn": "1476-4687"},       # eISSN 一致
            {"title": "C", "issn": "9999-9999", "eissn": ""},       # 非該当
        ]
        matched = scie.annotate_papers(papers, issn_set)
        assert matched == 2
        assert papers[0]["is_scie"] is True
        assert papers[1]["is_scie"] is True
        assert papers[2]["is_scie"] is False

    def test_empty_set_marks_all_false(self):
        papers = [{"issn": "0028-0836", "eissn": ""}]
        matched = scie.annotate_papers(papers, set())
        assert matched == 0
        assert papers[0]["is_scie"] is False


class TestDeriveIndexLabel:
    def test_parenthesized_abbreviation(self):
        assert scie.derive_index_label("Science Citation Index Expanded (SCIE).csv") == "SCIE"
        assert scie.derive_index_label("/path/to/Social Sciences Citation Index (SSCI).csv") == "SSCI"

    def test_no_parens_uses_stem(self):
        assert scie.derive_index_label("/x/ssci_list.csv") == "ssci_list"


class TestLoadIndexSets:
    def test_labels_derived_from_filenames(self, tmp_path):
        scie_f = tmp_path / "Index (SCIE).csv"
        scie_f.write_text("ISSN\n0028-0836\n", encoding="utf-8")
        ssci_f = tmp_path / "Index (SSCI).csv"
        ssci_f.write_text("ISSN\n0002-9602\n", encoding="utf-8")
        sets = scie.load_index_sets([str(scie_f), str(ssci_f)])
        assert set(sets.keys()) == {"SCIE", "SSCI"}
        assert "00280836" in sets["SCIE"]
        assert "00029602" in sets["SSCI"]


class TestAnnotatePapersIndexes:
    def test_assigns_index_labels(self):
        index_sets = {"SCIE": {"00280836"}, "SSCI": {"00029602"}}
        papers = [
            {"title": "A", "issn": "0028-0836", "eissn": ""},   # SCIE
            {"title": "B", "issn": "0002-9602", "eissn": ""},   # SSCI
            {"title": "C", "issn": "9999-9999", "eissn": ""},   # none
        ]
        matched = scie.annotate_papers_indexes(papers, index_sets)
        assert matched == 2
        assert papers[0]["wos_indexes"] == ["SCIE"]
        assert papers[0]["is_scie"] is True
        assert papers[1]["wos_indexes"] == ["SSCI"]
        assert papers[1]["is_scie"] is False
        assert papers[2]["wos_indexes"] == []

    def test_paper_in_multiple_indexes(self):
        index_sets = {"SCIE": {"00280836"}, "SSCI": {"00280836"}}
        papers = [{"title": "A", "issn": "0028-0836", "eissn": ""}]
        scie.annotate_papers_indexes(papers, index_sets)
        assert papers[0]["wos_indexes"] == ["SCIE", "SSCI"]
        assert papers[0]["is_scie"] is True
