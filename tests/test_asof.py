"""asof.py: 鮮度判定としきい値解決、比較セットの取得日一貫性。"""
import datetime

import pytest

from scopus_tools import asof

NOW = datetime.datetime(2026, 8, 16, 12, 0, 0)


def _iso(days_ago):
    return (NOW - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")


class TestStalePolicy:
    def test_defaults_are_per_api(self):
        p = asof.StalePolicy()
        assert p.days_for("scopus_search") == 30
        assert p.days_for("scopus_author_search") == 90
        assert p.days_for("kaken_project") == 30

    def test_unknown_api_falls_back_to_strictest(self):
        assert asof.StalePolicy().days_for("something_new") == asof.FALLBACK_STALE_DAYS

    def test_global_default_applies_to_unknown_only(self):
        p = asof.StalePolicy(default=1)
        assert p.days_for("brand_new") == 1
        # 既知の種別は既定表が勝つ(--stale-days-for で個別に変える)
        assert p.days_for("scopus_search") == 30

    def test_overrides_win(self):
        p = asof.StalePolicy(overrides={"scopus_search": 7})
        assert p.days_for("scopus_search") == 7
        assert p.days_for("kaken_project") == 30

    def test_env_is_read(self, monkeypatch):
        monkeypatch.setenv("SCOPUS_TOOLS_STALE_DAYS", "scopus_search=14,kaken_project=60")
        p = asof.StalePolicy()
        assert p.days_for("scopus_search") == 14
        assert p.days_for("kaken_project") == 60

    def test_env_single_value_sets_default(self, monkeypatch):
        monkeypatch.setenv("SCOPUS_TOOLS_STALE_DAYS", "3")
        assert asof.StalePolicy().days_for("unknown_api") == 3

    def test_explicit_overrides_beat_env(self, monkeypatch):
        monkeypatch.setenv("SCOPUS_TOOLS_STALE_DAYS", "scopus_search=14")
        p = asof.StalePolicy(overrides={"scopus_search": 2})
        assert p.days_for("scopus_search") == 2

    def test_invalid_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("SCOPUS_TOOLS_STALE_DAYS", "nonsense")
        assert asof.StalePolicy().days_for("scopus_search") == 30

    @pytest.mark.parametrize("raw,expected", [
        (["scopus_search=14"], {"scopus_search": 14}),
        (["a=1", "b=2"], {"a": 1, "b": 2}),
        (None, {}),
    ])
    def test_parse_overrides(self, raw, expected):
        assert asof.parse_overrides(raw) == expected

    @pytest.mark.parametrize("bad", ["scopus_search", "scopus_search=x"])
    def test_parse_overrides_rejects_bad_input(self, bad):
        with pytest.raises(ValueError):
            asof.parse_overrides([bad])


class TestDescribe:
    def test_same_age_differs_by_api(self):
        """同じ経過日数でも種別によって stale 判定が変わる。"""
        policy = asof.StalePolicy()
        fetched = _iso(45)
        assert asof.describe(fetched, "scopus_search", policy, now=NOW).stale is True
        assert asof.describe(fetched, "scopus_author_search", policy, now=NOW).stale is False

    def test_boundary(self):
        policy = asof.StalePolicy()
        assert asof.describe(_iso(30), "scopus_search", policy, now=NOW).stale is False
        assert asof.describe(_iso(31), "scopus_search", policy, now=NOW).stale is True

    def test_unknown_fetch_date_is_not_treated_as_fresh(self):
        entry = asof.describe(None, "scopus_search", asof.StalePolicy(), now=NOW)
        assert entry.known is False
        assert entry.age_days is None
        assert "unknown" in entry.note().lower()

    def test_note_mentions_threshold_and_api(self):
        entry = asof.describe(_iso(45), "scopus_search", asof.StalePolicy(), now=NOW)
        note = entry.note()
        assert "scopus_search" in note and "30-day" in note and "refresh=true" in note

    def test_fresh_fetch_note(self):
        entry = asof.describe(_iso(0), "scopus_search", asof.StalePolicy(),
                              cached=False, now=NOW)
        assert "just now" in entry.note()

    def test_to_dict_shape(self):
        d = asof.describe(_iso(3), "scopus_search", asof.StalePolicy(), now=NOW).to_dict()
        assert set(d) == {"fetched_at", "cached", "age_days", "stale", "api", "threshold_days"}
        assert d["threshold_days"] == 30


class TestSpread:
    def test_same_day_is_consistent(self):
        r = asof.spread([{"label": "A", "fetched_at": _iso(0)},
                         {"label": "B", "fetched_at": _iso(0.5)}], now=NOW)
        assert r["consistent"] is True
        assert asof.warning_lines(r) == []

    def test_boundary_at_one_day(self):
        exact = asof.spread([{"label": "A", "fetched_at": _iso(0)},
                             {"label": "B", "fetched_at": _iso(1.0)}], now=NOW)
        assert exact["consistent"] is True
        over = asof.spread([{"label": "A", "fetched_at": _iso(0)},
                            {"label": "B", "fetched_at": _iso(1.01)}], now=NOW)
        assert over["consistent"] is False

    def test_wide_spread_warns_with_names_and_dates(self):
        r = asof.spread([{"label": "岡村 寛之", "fetched_at": _iso(46)},
                         {"label": "田中 太郎", "fetched_at": _iso(0)}], now=NOW)
        assert r["consistent"] is False
        assert r["spread_days"] == pytest.approx(46, abs=0.1)
        lines = asof.warning_lines(r)
        text = "\n".join(lines)
        assert "岡村 寛之" in text and "田中 太郎" in text
        assert "not comparable" in text
        assert "--refresh" in text

    def test_unknown_dates_make_it_inconsistent(self):
        """取得日不明が混ざったら「そろっている」とは言えない。"""
        r = asof.spread([{"label": "A", "fetched_at": _iso(0)},
                         {"label": "B", "fetched_at": None}], now=NOW)
        assert r["consistent"] is False
        assert r["unknown_count"] == 1
        assert "B" in r["unknown_labels"]
        assert "fetch date unknown" in "\n".join(asof.warning_lines(r))

    def test_all_unknown(self):
        r = asof.spread([{"label": "A", "fetched_at": None}], now=NOW)
        assert r["consistent"] is None
        assert r["known_count"] == 0
        assert asof.warning_lines(r)      # 警告は出る

    def test_empty(self):
        r = asof.spread([], now=NOW)
        assert r["known_count"] == 0

    def test_single_entry_is_consistent(self):
        r = asof.spread([{"label": "A", "fetched_at": _iso(100)}], now=NOW)
        assert r["consistent"] is True     # 1 人なら比較の一貫性問題はない

    def test_per_entry_sorted_oldest_first(self):
        r = asof.spread([{"label": "new", "fetched_at": _iso(0)},
                         {"label": "old", "fetched_at": _iso(10)}], now=NOW)
        assert [e["label"] for e in r["per_entry"]] == ["old", "new"]


class TestFooter:
    def test_writes_to_given_stream(self):
        import io
        buf = io.StringIO()
        r = asof.spread([{"label": "A", "fetched_at": _iso(30)},
                         {"label": "B", "fetched_at": _iso(0)}], now=NOW)
        asof.print_asof_footer(r, stream=buf)
        assert "as-of warning" in buf.getvalue()

    def test_silent_when_consistent(self):
        import io
        buf = io.StringIO()
        r = asof.spread([{"label": "A", "fetched_at": _iso(0)}], now=NOW)
        asof.print_asof_footer(r, stream=buf)
        assert buf.getvalue() == ""
