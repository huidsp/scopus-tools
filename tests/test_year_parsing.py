"""発行年が欠けている論文の扱い。

Scopus の `prism:coverDate` は、キーごと無いこともあれば null や空文字で返ることもある。
以前は `entry.get("prism:coverDate", "0000")[:4]` としており、既定値が効くのは
キーが無いときだけだったため、null で TypeError、空文字で ValueError になった。
これは `search_papers` のページングループ内で送出されるので、1 件の欠損が
その著者の取得全体を落とす。
"""
from scopus_tools.api import parse_entry
from scopus_tools.core import summarize_papers


def _entry(**over):
    e = {"dc:title": "T", "prism:publicationName": "J", "citedby-count": "3"}
    e.update(over)
    return e


class TestCoverDate:

    def test_normal_date(self):
        assert parse_entry(_entry(**{"prism:coverDate": "2024-05-01"}))["year"] == 2024

    def test_missing_key(self):
        assert parse_entry(_entry())["year"] == 0

    def test_null_value(self):
        """JSON null。以前は TypeError で取得全体が落ちていた。"""
        assert parse_entry(_entry(**{"prism:coverDate": None}))["year"] == 0

    def test_empty_string(self):
        """以前は int("") の ValueError。"""
        assert parse_entry(_entry(**{"prism:coverDate": ""}))["year"] == 0

    def test_garbage_value(self):
        assert parse_entry(_entry(**{"prism:coverDate": "n/a"}))["year"] == 0


class TestUnknownYearDoesNotSkewTheSummary:

    def test_year_zero_is_excluded_from_start_year(self):
        """year=0 を最小値に含めると research_years が current_year+1 になり、
        報告書に「研究年数 2027 年」のような値が出る。"""
        papers = [
            {"year": 0, "citations": 1, "is_first_author": False},
            {"year": 2020, "citations": 5, "is_first_author": True},
        ]
        s = summarize_papers(papers, year_range=(2021, 2025))
        assert s["start_year"] == 2020
        assert 0 < s["research_years"] < 100
        # 年不明の論文も総数からは落とさない(業績自体は存在する)
        assert s["total_count"] == 2

    def test_all_years_unknown(self):
        s = summarize_papers([{"year": 0, "citations": 1}], year_range=(2021, 2025))
        assert s["start_year"] is None
        assert s["research_years"] == 0
        assert s["total_count"] == 1
