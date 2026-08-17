"""データの鮮度(as-of 日付)と、比較セット内での取得日の一貫性。

人事選考で複数の研究者を比較するとき、**取得年月日がそろっている必要がある**。
被引用数は時間とともに増えるので、6 月に取った人と 8 月に取った人を同じ表に並べると
後者が不当に有利になる。

方針は「**自動失効させず、古くなったら警告する**」。勝手に一部だけ再取得すると
取得日がばらけて比較が壊れるため、再取得は利用者が明示的に行う(`--refresh`)。

しきい値は **API 種別ごと**に設定できる。データの変化速度が種別で全く違い、
単一のしきい値だと「名前→ID の対応が半年前」という無害な事実にまで警告が出て
ノイズになるため。
"""

import datetime
import logging
import os

logger = logging.getLogger(__name__)

# 種別ごとの既定しきい値(日)。いずれも上書き可能。
DEFAULT_STALE_DAYS = {
    "scopus_search":          30,    # 被引用数・論文数が動く。ただし月単位でしか
                                    # 意味のある変化をしないので 30 日
                                    # (比較セットの取得日ずれは SPREAD_TOLERANCE_DAYS
                                    #  が別途 1 日で見張るので、ここを短くする必要はない)
    "scopus_author_search":   90,    # 名前 → Scopus ID の対応はほぼ変わらない
    "scopus_author_retrieval": 90,   # 著者の姓名。ほぼ変わらない
    "kaken_project":          30,    # 採択課題は年度単位で増える
    "kaken_researcher":       90,    # 研究者番号の対応はほぼ変わらない
    "openalex_work":          30,    # 被引用数が動く。Scopus 側と揃える
    "openalex_author":        90,    # 著者 ID・所属履歴。ほぼ変わらない
}

# 未知の種別はいちばん厳しい側に倒す(見逃すより警告する)
FALLBACK_STALE_DAYS = 7

# 比較セットとして「同じ日に取った」と見なす許容幅(日)
SPREAD_TOLERANCE_DAYS = 1.0


class AsOf:
    """1 件のデータの鮮度。どのしきい値で判定したかも保持する。"""

    __slots__ = ("fetched_at", "cached", "age_days", "stale", "api", "threshold_days")

    def __init__(self, fetched_at, cached, age_days, stale, api, threshold_days):
        self.fetched_at = fetched_at
        self.cached = cached
        self.age_days = age_days
        self.stale = stale
        self.api = api
        self.threshold_days = threshold_days

    @property
    def known(self):
        return self.fetched_at is not None

    def to_dict(self):
        return {
            "fetched_at": self.fetched_at,
            "cached": self.cached,
            "age_days": None if self.age_days is None else round(self.age_days, 2),
            "stale": self.stale,
            "api": self.api,
            "threshold_days": self.threshold_days,
        }

    def note(self):
        """モデル / 利用者に見せる 1 文。生の timestamp より自然文の方が伝わる。"""
        if not self.known:
            return "The fetch date of this data is unknown; treat it as potentially outdated."
        date = self.fetched_at.split("T")[0]
        if not self.cached:
            return f"Fetched just now ({date})."
        age = int(self.age_days or 0)
        base = f"This data is as of {date} ({age} day{'s' if age != 1 else ''} old)."
        if self.stale:
            base += (f" That exceeds the {self.threshold_days}-day freshness threshold for "
                     f"{self.api}; pass refresh=true for current numbers.")
        return base


class StalePolicy:
    """API 種別 → しきい値の解決。

    優先順位(後勝ち): 既定値 < 環境変数 < default 引数 < overrides 引数
    """

    def __init__(self, overrides=None, default=None):
        self.table = dict(DEFAULT_STALE_DAYS)
        self.default = None

        env_default, env_overrides = _parse_env_stale_days()
        if env_default is not None:
            self.default = env_default
        self.table.update(env_overrides)

        if default is not None:
            self.default = int(default)
        if overrides:
            self.table.update({k: int(v) for k, v in dict(overrides).items()})

    def days_for(self, api):
        if api in self.table:
            return self.table[api]
        if self.default is not None:
            return self.default
        return FALLBACK_STALE_DAYS

    def describe_all(self):
        return dict(sorted(self.table.items()))


def _parse_env_stale_days():
    """$SCOPUS_TOOLS_STALE_DAYS を (default, overrides) に解釈する。

    "7" のような単一値、"scopus_search=7,kaken_project=30" のような種別指定の両方を受ける。
    """
    raw = (os.getenv("SCOPUS_TOOLS_STALE_DAYS") or "").strip()
    if not raw:
        return None, {}
    if "=" not in raw:
        try:
            return int(raw), {}
        except ValueError:
            logger.warning("Ignoring invalid SCOPUS_TOOLS_STALE_DAYS=%r", raw)
            return None, {}
    overrides = {}
    for chunk in raw.split(","):
        if not chunk.strip():
            continue
        key, _, value = chunk.partition("=")
        try:
            overrides[key.strip()] = int(value)
        except ValueError:
            logger.warning("Ignoring invalid stale-days entry %r", chunk)
    return None, overrides


def parse_overrides(pairs):
    """CLI の `--stale-days-for API=N` (複数指定可) を dict にする。"""
    out = {}
    for item in pairs or []:
        key, sep, value = str(item).partition("=")
        if not sep:
            raise ValueError(f"--stale-days-for expects API=DAYS (got {item!r})")
        try:
            out[key.strip()] = int(value)
        except ValueError:
            raise ValueError(f"--stale-days-for: {value!r} is not an integer")
    return out


def _to_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def describe(fetched_at, api="generic", policy=None, cached=True, now=None):
    """1 件のデータの鮮度を判定する。"""
    policy = policy or StalePolicy()
    threshold = policy.days_for(api)
    dt = _to_datetime(fetched_at)
    if dt is None:
        # 取得日不明。「新しい」と扱ってはいけない。
        return AsOf(None, cached, None, False, api, threshold)
    now = now or datetime.datetime.now()
    age_days = (now - dt).total_seconds() / 86400.0
    return AsOf(dt.isoformat(timespec="seconds"), cached, age_days,
                age_days > threshold, api, threshold)


def spread(entries, now=None):
    """複数エントリの取得日の散らばりを見る。

    entries は `{"label": str, "fetched_at": iso|None}` のリスト。
    戻り値には `consistent`(同じ日に取ったか)と、判定に使えなかった件数を含む。
    """
    now = now or datetime.datetime.now()
    known, unknown = [], []
    for e in entries or []:
        dt = _to_datetime(e.get("fetched_at"))
        (known if dt else unknown).append((e.get("label"), dt))

    if not known:
        return {
            "consistent": None, "spread_days": None,
            "oldest": None, "newest": None,
            "known_count": 0, "unknown_count": len(unknown),
            "unknown_labels": [lbl for lbl, _ in unknown],
            "per_entry": [{"label": lbl, "fetched_at": None, "age_days": None}
                          for lbl, _ in unknown],
        }

    oldest = min(dt for _, dt in known)
    newest = max(dt for _, dt in known)
    spread_days = (newest - oldest).total_seconds() / 86400.0
    per_entry = sorted(
        [{"label": lbl,
          "fetched_at": dt.isoformat(timespec="seconds"),
          "age_days": round((now - dt).total_seconds() / 86400.0, 2)}
         for lbl, dt in known],
        key=lambda r: r["fetched_at"])
    per_entry += [{"label": lbl, "fetched_at": None, "age_days": None} for lbl, _ in unknown]

    # 取得日不明が混ざっていたら「一貫している」とは言えない
    consistent = spread_days <= SPREAD_TOLERANCE_DAYS and not unknown
    return {
        "consistent": consistent,
        "spread_days": round(spread_days, 2),
        "oldest": oldest.isoformat(timespec="seconds"),
        "newest": newest.isoformat(timespec="seconds"),
        "known_count": len(known),
        "unknown_count": len(unknown),
        "unknown_labels": [lbl for lbl, _ in unknown],
        "per_entry": per_entry,
    }


def warning_lines(report):
    """`spread()` の結果を人が読む警告行にする。問題なければ空リスト。"""
    if not report or report.get("consistent") is True:
        return []
    lines = []
    if report.get("known_count", 0) == 0:
        lines.append("[as-of warning] No fetch dates recorded for this comparison; "
                     "the numbers may come from different dates.")
    else:
        lines.append(
            f"[as-of warning] The compared data was not fetched on the same date "
            f"(spread {report['spread_days']} days). Citation counts are not comparable.")
    for row in report.get("per_entry", []):
        if row["fetched_at"]:
            date = row["fetched_at"].split("T")[0]
            lines.append(f"    {row['label']}: {date} ({int(row['age_days'])} days old)")
        else:
            lines.append(f"    {row['label']}: fetch date unknown")
    lines.append("  Re-fetch the older entries with --refresh so every figure "
                 "is as of the same date.")
    return lines


def print_asof_footer(report, stream=None):
    """CLI 用。**必ず stderr**(--format json/csv の stdout を汚さないため)。"""
    import sys

    lines = warning_lines(report)
    if not lines:
        return
    out = stream or sys.stderr
    for line in lines:
        print(line, file=out)
