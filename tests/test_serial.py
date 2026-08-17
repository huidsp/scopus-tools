"""Scopus Serial Title の雑誌指標。

このモジュールの存在理由は「**論文の出版年に合わせた指標を出せること**」なので、
年の選択を厚くテストする。JCR を常用から外したのがまさにそこができないため。
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from scopus_tools import api, mcp_server, serial
from scopus_tools.serial import (annotate_papers_metrics, parse_serial_entry,
                                 pick_metrics_for_year, quartile_from_percentile,
                                 summarize_metrics)


def _year(year, citescore, status="Complete", ranks=(("2213", 2, 99),)):
    return {
        "@year": str(year), "@status": status,
        "citeScoreInformationList": [{"citeScoreInfo": [{
            "docType": "all",
            "citeScore": str(citescore),
            "scholarlyOutput": "2,825",
            "percentCited": "93",
            "citeScoreSubjectRank": [
                {"subjectCode": c, "rank": str(r), "percentile": str(p)}
                for c, r, p in ranks],
        }]}],
    }


def _entry(issn="1234-5678", eissn="8765-4321", years=None, sjr=2.838, snip=2.759,
           agg="journal"):
    years = years if years is not None else [_year(2025, 21.9), _year(2021, 10.2)]
    e = {
        "prism:issn": issn, "prism:eIssn": eissn,
        "dc:title": "Journal of Testing", "dc:publisher": "PUB",
        "prism:aggregationType": agg,
        "subject-area": [
            {"@code": "2213", "@abbrev": "ENGI", "$": "Safety, Risk, Reliability"},
            {"@code": "2209", "@abbrev": "ENGI", "$": "Industrial Engineering"},
        ],
        "citeScoreYearInfoList": {"citeScoreYearInfo": years},
    }
    if sjr is not None:
        e["SJRList"] = {"SJR": [{"@year": "2025", "$": str(sjr)}]}
    if snip is not None:
        e["SNIPList"] = {"SNIP": [{"@year": "2025", "$": str(snip)}]}
    return e


class TestParseEntry:

    def test_indexed_by_both_issn_and_eissn(self):
        assert parse_serial_entry(_entry())["issns"] == ["12345678", "87654321"]

    def test_year_history_is_kept_not_flattened(self):
        """最新年だけに畳むと、このモジュールの意味が無くなる。"""
        rec = parse_serial_entry(_entry())
        assert set(rec["citescore_by_year"]) == {2021, 2025}
        assert rec["citescore_by_year"][2021]["citescore"] == 10.2

    def test_subject_names_are_resolved_from_codes(self):
        rec = parse_serial_entry(_entry())
        ranks = rec["citescore_by_year"][2025]["ranks"]
        assert ranks[0]["code"] == "2213"
        assert ranks[0]["name"] == "Safety, Risk, Reliability"

    def test_sjr_snip_carry_their_year(self):
        """SJR/SNIP は最新年しか返らない。出版年に合わせられないことを示すため。"""
        rec = parse_serial_entry(_entry())
        assert rec["sjr"] == {"year": 2025, "value": 2.838}
        assert rec["snip"] == {"year": 2025, "value": 2.759}

    def test_missing_metrics_do_not_crash(self):
        rec = parse_serial_entry(_entry(years=[], sjr=None, snip=None))
        assert rec["citescore_by_year"] == {} and rec["sjr"] is None

    def test_single_element_returned_as_dict_not_list(self):
        """Elsevier は要素 1 つのとき配列でなく dict を返すことがある。"""
        e = _entry()
        e["citeScoreYearInfoList"]["citeScoreYearInfo"] = _year(2025, 21.9)
        e["subject-area"] = {"@code": "2213", "@abbrev": "ENGI", "$": "Safety"}
        rec = parse_serial_entry(e)
        assert 2025 in rec["citescore_by_year"]

    def test_thousands_separator_in_output_count(self):
        rec = parse_serial_entry(_entry())
        assert rec["citescore_by_year"][2025]["scholarly_output"] == 2825


class TestYearSelection:
    """このモジュールの核。どの年の値かを黙って隠さないこと。"""

    def _rec(self):
        return parse_serial_entry(_entry(years=[
            _year(2025, 21.9), _year(2021, 10.2), _year(2013, 5.2)]))

    def test_exact_year(self):
        m = pick_metrics_for_year(self._rec(), 2021)
        assert m["citescore"] == 10.2
        assert m["metric_year"] == 2021
        assert m["year_match"] == "exact"

    def test_same_journal_differs_by_publication_year(self):
        """同じ誌でも古い論文には当時の値が付くこと。"""
        rec = self._rec()
        assert pick_metrics_for_year(rec, 2013)["citescore"] == 5.2
        assert pick_metrics_for_year(rec, 2025)["citescore"] == 21.9

    def test_year_before_coverage_falls_back_to_nearest(self):
        m = pick_metrics_for_year(self._rec(), 2005)
        assert m["metric_year"] == 2013
        assert m["year_match"] == "nearest"

    def test_year_after_coverage_falls_back_to_nearest(self):
        m = pick_metrics_for_year(self._rec(), 2030)
        assert m["metric_year"] == 2025
        assert m["year_match"] == "nearest"

    def test_tie_prefers_the_newer_year(self):
        """2017 は 2013 とも 2021 とも 4 年差。古い値を当てない。"""
        m = pick_metrics_for_year(self._rec(), 2017)
        assert m["metric_year"] == 2021

    def test_no_year_given_uses_latest(self):
        m = pick_metrics_for_year(self._rec(), None)
        assert m["metric_year"] == 2025 and m["year_match"] == "nearest"

    def test_journal_without_any_citescore(self):
        m = pick_metrics_for_year(parse_serial_entry(_entry(years=[])), 2021)
        assert m["year_match"] == "none"
        assert m["citescore"] is None          # 0 にしない

    def test_in_progress_year_is_flagged_provisional(self):
        rec = parse_serial_entry(_entry(years=[_year(2026, 19.0, status="In-Progress")]))
        m = pick_metrics_for_year(rec, 2026)
        assert m["provisional"] is True

    def test_complete_year_is_not_provisional(self):
        assert pick_metrics_for_year(self._rec(), 2021)["provisional"] is False


class TestSubjectRanks:

    def test_best_percentile_is_representative(self):
        rec = parse_serial_entry(_entry(years=[
            _year(2025, 21.9, ranks=(("2209", 10, 60), ("2213", 2, 99)))]))
        m = pick_metrics_for_year(rec, 2025)
        assert m["percentile"] == 99 and m["quartile"] == "Q1"
        assert len(m["ranks"]) == 2          # 内訳は残す

    @pytest.mark.parametrize("pct,expected", [
        (100, "Q1"), (75, "Q1"), (74, "Q2"), (50, "Q2"),
        (49, "Q3"), (25, "Q3"), (24, "Q4"), (0, "Q4"), (None, None),
    ])
    def test_quartile_boundaries(self, pct, expected):
        assert quartile_from_percentile(pct) == expected


class TestAnnotate:

    def _table(self):
        rec = parse_serial_entry(_entry(years=[_year(2025, 21.9), _year(2013, 5.2)]))
        return {i: rec for i in rec["issns"]}

    def test_uses_publication_year_by_default(self):
        papers = [{"issn": "1234-5678", "year": 2013},
                  {"issn": "1234-5678", "year": 2025}]
        assert annotate_papers_metrics(papers, self._table()) == 2
        assert papers[0]["metrics"]["citescore"] == 5.2
        assert papers[1]["metrics"]["citescore"] == 21.9

    def test_can_force_latest_year(self):
        papers = [{"issn": "1234-5678", "year": 2013}]
        annotate_papers_metrics(papers, self._table(), use_publication_year=False)
        assert papers[0]["metrics"]["metric_year"] == 2025

    def test_matches_on_eissn_too(self):
        papers = [{"issn": "", "eissn": "8765-4321", "year": 2025}]
        assert annotate_papers_metrics(papers, self._table()) == 1

    def test_unknown_journal_gets_none(self):
        papers = [{"issn": "0000-0000", "year": 2025}]
        assert annotate_papers_metrics(papers, self._table()) == 0
        assert papers[0]["metrics"] is None

    def test_year_zero_is_treated_as_unknown(self):
        """発行年が取れなかった論文(year=0)は最新年で代用する。"""
        papers = [{"issn": "1234-5678", "year": 0}]
        annotate_papers_metrics(papers, self._table())
        assert papers[0]["metrics"]["year_match"] == "nearest"

    def test_empty_table_is_a_no_op(self):
        papers = [{"issn": "1234-5678", "year": 2025}]
        assert annotate_papers_metrics(papers, {}) == 0


class TestSummary:

    def _papers(self, specs):
        return [{"metrics": m} for m in specs]

    def test_no_primary_metric_all_four_side_by_side(self):
        """ユーザの選択どおり、主指標を作らず 4 指標を対等に並べる。"""
        s = summarize_metrics(self._papers([{
            "citescore": 10.0, "percentile": 90, "quartile": "Q1",
            "sjr": {"year": 2025, "value": 2.0}, "snip": {"year": 2025, "value": 1.5},
            "year_match": "exact"}]))
        for key in ("citescore", "percentile", "sjr", "snip"):
            assert set(s[key]) == {"count", "median", "max"}

    def test_median_not_skewed_by_an_outlier(self):
        s = summarize_metrics(self._papers([
            {"citescore": c, "year_match": "exact"} for c in (1, 2, 3, 400)]))
        assert s["citescore"]["median"] == 2.5
        assert s["citescore"]["max"] == 400

    def test_year_match_breakdown_is_reported(self):
        """nearest が多ければ出版年の指標として読んではいけない。"""
        s = summarize_metrics(self._papers([
            {"citescore": 1, "year_match": "exact"},
            {"citescore": 2, "year_match": "nearest"},
            None]))
        assert s["year_match"]["exact"] == 1
        assert s["year_match"]["nearest"] == 1
        assert s["year_match"]["none"] == 1
        assert s["without_metrics"] == 1

    def test_provisional_counted(self):
        s = summarize_metrics(self._papers([
            {"citescore": 1, "year_match": "exact", "provisional": True}]))
        assert s["provisional_count"] == 1

    def test_note_says_citescore_is_not_the_impact_factor(self):
        note = summarize_metrics([])["note"]
        assert "not the Journal Impact Factor" in note
        assert "Percentile" in note

    def test_empty(self):
        s = summarize_metrics([])
        assert s["citescore"]["median"] is None and s["papers"] == 0


class TestClientBatching:

    def _client(self, responses):
        client = api.ScopusClient(api_key="dummy", http=MagicMock())
        client._http.get.side_effect = responses
        return client

    def _resp(self, entries, status=200):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = {"serial-metadata-response": {"entry": entries}}
        return r

    def test_batches_of_25(self):
        issns = [f"1234-{i:04d}" for i in range(60)]
        client = self._client([self._resp([]), self._resp([]), self._resp([])])
        client.get_serial_metrics(issns)
        assert client._http.get.call_count == 3          # 25 + 25 + 10
        sent = client._http.get.call_args_list[0][1]["params"]["issn"]
        assert len(sent.split(",")) == 25

    def test_matched_by_issn_not_by_position(self):
        """未知の ISSN は黙って返ってこない。順番で対応付けると全部ずれる。"""
        client = self._client([self._resp([_entry(issn="2222-2222", eissn="")])])
        table, missing = client.get_serial_metrics(["1111-1111", "2222-2222"])
        assert "22222222" in table
        assert missing == ["11111111"]
        assert "11111111" not in table

    def test_duplicate_issns_requested_once(self):
        client = self._client([self._resp([])])
        client.get_serial_metrics(["1234-5678", "1234-5678", "12345678"])
        assert client._http.get.call_args[1]["params"]["issn"] == "12345678"

    def test_http_error_does_not_abort_other_batches(self):
        issns = [f"1234-{i:04d}" for i in range(30)]
        client = self._client([self._resp([], status=500),
                               self._resp([_entry(issn="1234-0029", eissn="")])])
        table, missing = client.get_serial_metrics(issns)
        assert client._http.get.call_count == 2
        assert "12340029" in table
        assert len(missing) == 29

    def test_empty_input(self):
        client = self._client([])
        assert client.get_serial_metrics([]) == ({}, [])
        assert client._http.get.call_count == 0

    def test_uses_the_serial_api_family(self):
        client = self._client([self._resp([])])
        client.get_serial_metrics(["1234-5678"])
        assert client._http.get.call_args[1]["api"] == "scopus_serial"


class TestMcpTool:

    def test_missing_key_returns_error(self):
        with patch.dict(os.environ, {}, clear=True):
            mcp_server._scopus_client = None
            assert "SCOPUS_API_KEY" in mcp_server.journal_metrics("1234-5678")["error"]

    def test_reports_unresolved_issns_without_calling_them_errors(self, monkeypatch):
        client = MagicMock()
        client._http.refresh = False
        client.get_serial_metrics.return_value = ({}, ["99999999"])
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "k"}, clear=True):
            r = mcp_server.journal_metrics("9999-9999")
        assert r["unresolved"] == ["99999999"]
        assert "not an error" in r["unresolved_note"]
        assert "error" not in r

    def test_year_is_passed_through(self, monkeypatch):
        rec = parse_serial_entry(_entry(years=[_year(2025, 21.9), _year(2013, 5.2)]))
        client = MagicMock()
        client._http.refresh = False
        client.get_serial_metrics.return_value = ({i: rec for i in rec["issns"]}, [])
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "k"}, clear=True):
            r = mcp_server.journal_metrics("1234-5678", year=2013)
        assert r["journals"][0]["citescore"] == 5.2
        assert r["journals"][0]["metric_year"] == 2013

    def test_note_warns_citescore_is_not_if(self, monkeypatch):
        client = MagicMock()
        client._http.refresh = False
        client.get_serial_metrics.return_value = ({}, [])
        monkeypatch.setattr(mcp_server, "_scopus_client", client)
        with patch.dict(os.environ, {"SCOPUS_API_KEY": "k"}, clear=True):
            r = mcp_server.journal_metrics("1234-5678")
        assert "not the Journal Impact Factor" in r["note"]
