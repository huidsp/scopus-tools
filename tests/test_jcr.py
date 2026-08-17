"""JCR エクスポート CSV の読み込み。

実物の書き出しには素直でない点が多く、どれも黙って壊れる(例外にならず
静かに 0 件や 1 件に潰れる)ので、確認した落とし穴を 1 つずつ固定する。
"""
import pytest

from scopus_tools import jcr


HEADER = ("Journal name,JCR Abbreviation,Publisher,ISSN,eISSN,Category,Edition,"
          "Total Citations,2025 JIF,JIF Quartile,2025 JCI,% of Citable OA")
BANNER = ('"Journal Data Filtered By:  Selected Editions: SCIE;SSCI;AHCI;ESCI '
          'Selected JCR Year: 2025 Selected Category Schema: WOS"')
FOOTER = ["", "Copyright (c) 2026 Clarivate ",
          "By exporting the selected data; you agree to the data usage policy "]


def write_csv(tmp_path, rows, header=HEADER, banner=BANNER, footer=True, name="JCR.csv"):
    lines = ([banner, ""] if banner is not None else []) + [header] + list(rows)
    if footer:
        lines += FOOTER
    path = tmp_path / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def row(journal="JOURNAL OF TESTING", issn="1234-5678", eissn="8765-4321",
        category="COMPUTER SCIENCE", edition="SCIE", cites='"1,234"',
        jif="13.7", quartile="Q1", jci="2.5", oa='"100"%,'):
    return (f'"{journal}","ABBR","PUBLISHER","{issn}","{eissn}","{category}",'
            f'"{edition}",{cites},"{jif}","{quartile}","{jci}",{oa}')


class TestFileStructure:

    def test_banner_and_blank_line_are_skipped(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row()]))
        assert len(t) == 2                      # ISSN と eISSN の両方で索引される
        assert next(iter(t.values()))["journal"] == "JOURNAL OF TESTING"

    def test_copyright_footer_is_dropped(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row()]))
        names = {r["journal"] for r in t.values()}
        assert not any(n.lower().startswith(("copyright", "by exporting")) for n in names)

    def test_works_without_a_banner(self, tmp_path):
        """書き出し設定によってはバナーが無い。ヘッダを探して対応する。"""
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row()], banner=None))
        assert len(t) == 2

    def test_missing_header_is_skipped_not_crashed(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("nothing useful here\n1,2,3\n", encoding="utf-8")
        assert jcr.load_jcr_csv(str(p)) == {}

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("", encoding="utf-8")
        assert jcr.load_jcr_csv(str(p)) == {}


class TestColumnHazards:

    def test_year_is_taken_from_the_banner(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row()]))
        assert next(iter(t.values()))["jcr_year"] == 2025

    def test_jif_column_is_matched_by_suffix_not_exact_name(self, tmp_path):
        """列名に年が入る(2025 JIF)。翌年の書き出しでも壊れないこと。"""
        header = HEADER.replace("2025 JIF", "2031 JIF").replace("2025 JCI", "2031 JCI")
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row()], header=header, banner=None))
        rec = next(iter(t.values()))
        assert rec["jif"] == 13.7
        assert rec["jcr_year"] == 2031          # 列名からも年を拾う

    def test_thousands_separator_and_percent_do_not_break_numbers(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row(jif="1,234.5")]))
        assert next(iter(t.values()))["jif"] == 1234.5

    def test_less_than_notation(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row(jif="<0.1")]))
        assert next(iter(t.values()))["jif"] == 0.1

    def test_na_jif_becomes_none_not_zero(self, tmp_path):
        """0.0 にすると「IF が非常に低い雑誌」と区別できなくなる。"""
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row(jif="N/A")]))
        assert next(iter(t.values()))["jif"] is None


class TestIssnHandling:

    def test_indexed_by_both_issn_and_eissn(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row()]))
        assert "12345678" in t and "87654321" in t

    def test_issn_literal_na_falls_back_to_eissn(self, tmp_path):
        """ISSN 列が文字列 "N/A" のことがある。キーにすると全誌が 1 件に潰れる。"""
        t = jcr.load_jcr_csv(write_csv(tmp_path, [
            row(journal="A", issn="N/A", eissn="1111-1111"),
            row(journal="B", issn="N/A", eissn="2222-2222"),
        ]))
        assert set(t) == {"11111111", "22222222"}
        assert {r["journal"] for r in t.values()} == {"A", "B"}

    def test_row_without_any_issn_is_dropped(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row(issn="N/A", eissn="N/A")]))
        assert t == {}


class TestQuartilePerCategory:

    def test_one_journal_many_categories(self, tmp_path):
        """1 行 = 1 雑誌 x 1 カテゴリ。分位はカテゴリごとに違いうる。"""
        t = jcr.load_jcr_csv(write_csv(tmp_path, [
            row(category="LINGUISTICS", quartile="Q1"),
            row(category="LANGUAGE", quartile="N/A"),
            row(category="COMPUTER SCIENCE", quartile="Q2"),
        ]))
        rec = t["12345678"]
        assert rec["quartile"] == "Q1"                     # 代表値は最良
        assert len(rec["categories"]) == 3
        assert {c["quartile"] for c in rec["categories"]} == {"Q1", None, "Q2"}

    def test_best_quartile_when_no_q1(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [
            row(category="A", quartile="Q3"), row(category="B", quartile="Q2")]))
        assert t["12345678"]["quartile"] == "Q2"

    def test_all_na_quartiles(self, tmp_path):
        t = jcr.load_jcr_csv(write_csv(tmp_path, [row(quartile="N/A")]))
        assert t["12345678"]["quartile"] is None


class TestMergingFiles:

    def test_categories_merge_across_files(self, tmp_path):
        """カテゴリ別に書き出すと 1 誌が複数ファイルにまたがる。"""
        a = write_csv(tmp_path, [row(category="A", quartile="Q2")], name="a.csv")
        b = write_csv(tmp_path, [row(category="B", quartile="Q1")], name="b.csv")
        merged = jcr.load_jcr_tables([a, b])
        rec = merged["12345678"]
        assert {c["category"] for c in rec["categories"]} == {"A", "B"}
        assert rec["quartile"] == "Q1"

    def test_duplicate_category_is_not_doubled(self, tmp_path):
        a = write_csv(tmp_path, [row(category="A")], name="a.csv")
        b = write_csv(tmp_path, [row(category="A")], name="b.csv")
        assert len(jcr.load_jcr_tables([a, b])["12345678"]["categories"]) == 1

    def test_mixed_jcr_years_warn(self, tmp_path, caplog):
        """別の JCR 年のファイルを黙って混ぜない。"""
        a = write_csv(tmp_path, [row()], name="a.csv")
        b = write_csv(tmp_path, [row()], name="b.csv",
                      banner=BANNER.replace("2025", "2024"))
        with caplog.at_level("WARNING"):
            jcr.load_jcr_tables([a, b])
        assert any("mixes JCR" in r.getMessage() for r in caplog.records)

    def test_unreadable_file_is_skipped(self, tmp_path):
        good = write_csv(tmp_path, [row()], name="g.csv")
        assert jcr.load_jcr_tables([good, str(tmp_path / "missing.csv")])


class TestAnnotation:

    def _table(self, tmp_path):
        return jcr.load_jcr_tables([write_csv(tmp_path, [row()])])

    def test_annotates_by_issn(self, tmp_path):
        papers = [{"issn": "1234-5678", "eissn": ""}]
        assert jcr.annotate_papers_jcr(papers, self._table(tmp_path)) == 1
        assert papers[0]["jcr"]["jif"] == 13.7
        assert papers[0]["jcr"]["quartile"] == "Q1"
        assert papers[0]["jcr"]["jcr_year"] == 2025

    def test_annotates_by_eissn_when_issn_absent(self, tmp_path):
        papers = [{"issn": "", "eissn": "8765-4321"}]
        assert jcr.annotate_papers_jcr(papers, self._table(tmp_path)) == 1

    def test_unmatched_paper_gets_none_not_missing_key(self, tmp_path):
        papers = [{"issn": "0000-0000", "eissn": ""}]
        assert jcr.annotate_papers_jcr(papers, self._table(tmp_path)) == 0
        assert papers[0]["jcr"] is None

    def test_empty_table_is_a_no_op(self):
        papers = [{"issn": "1234-5678"}]
        assert jcr.annotate_papers_jcr(papers, {}) == 0
        assert "jcr" not in papers[0]


class TestSummary:

    def _papers(self, jifs, quartiles):
        return [{"jcr": {"jif": j, "quartile": q}} for j, q in zip(jifs, quartiles)]

    def test_median_not_skewed_by_one_outlier(self):
        s = jcr.summarize_jcr(self._papers([1, 2, 3, 400], ["Q2"] * 4))
        assert s["jif_median"] == 2.5
        assert s["jif_max"] == 400

    def test_quartile_counts(self):
        s = jcr.summarize_jcr(self._papers([1, 1, 1], ["Q1", "Q1", "Q3"]))
        assert s["quartiles"]["Q1"] == 2 and s["quartiles"]["Q3"] == 1

    def test_papers_without_jcr_are_counted_separately(self):
        s = jcr.summarize_jcr([{"jcr": None}, {"jcr": {"jif": 5, "quartile": "Q1"}}])
        assert s["with_jif"] == 1 and s["without_jif"] == 1
        assert s["quartile_unknown"] == 1

    def test_empty(self):
        s = jcr.summarize_jcr([])
        assert s["with_jif"] == 0 and s["jif_median"] is None

    def test_note_warns_that_jif_is_journal_level(self):
        assert "journal-level" in jcr.summarize_jcr([])["note"]


class TestDiscovery:

    def test_explicit_list_wins(self, tmp_path):
        p = write_csv(tmp_path, [row()])
        assert jcr.resolve_jcr_paths(jcr_list=[p]) == [p]

    def test_dir_globs_csv(self, tmp_path):
        write_csv(tmp_path, [row()], name="a.csv")
        write_csv(tmp_path, [row()], name="b.csv")
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        found = jcr.resolve_jcr_paths(jcr_dir=str(tmp_path))
        assert len(found) == 2 and all(f.endswith(".csv") for f in found)

    def test_discover_returns_empty_without_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert jcr.discover_jcr_table() == {}


class TestParseJcrYear:

    @pytest.mark.parametrize("text,expected", [
        ("Selected JCR Year: 2025 Selected", 2025),
        ("JCR Year:2019", 2019),
        ("no year here", None),
        (None, None),
    ])
    def test_year(self, text, expected):
        assert jcr.parse_jcr_year(text) == expected
