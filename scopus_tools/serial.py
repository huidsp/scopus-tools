"""Scopus Serial Title API が返す雑誌指標(CiteScore / パーセンタイル / SJR / SNIP)。

`scie.py`(収録版)・`jcr.py`(JCR の IF)と同じ役回りの純関数モジュール。通信はしない。
取得は `api.ScopusClient.get_serial_metrics()`(Scopus のネットワーク境界を 1 つに保つため)。

**なぜ JCR ではなくこちらを常用するか**(いずれも実測)

  - **出版年の指標が取れる。** CiteScore は 2011–2026 の 16 年分が 1 回の応答に入る。
    JCR は最新年しか書き出せず、2021 年の論文に 2025 年の IF を当てるしかなかった。
  - **カバー率が高い。** 論文ベースで 91%(JCR は 42%)。会議録シリーズ(LNCS 等)や
    conferenceproceeding にも CiteScore が付くのが決定的な差。
  - **手作業が要らない。** 既存の `SCOPUS_API_KEY` で引け、クォータは週 20,000 と
    `scopus_search` の枠とは別。715 誌でも 29 リクエスト。

**CiteScore は Impact Factor ではない。** 4 年窓で分母が全文献種別なので IF より大きく
出る。指標名を置き換えて報告してはいけない。一方 **パーセンタイル**は「その分野で
上位何 %」なので、分野をまたいだ比較には生値より適している。

### 応答の構造(実測)

    entry
      prism:issn / prism:eIssn / dc:title / prism:aggregationType
      subject-area[]                     … {@code, @abbrev, $=名前}
      SJRList.SJR[]  / SNIPList.SNIP[]   … {@year, $}  ※**最新年のみ**返ることが多い
      citeScoreYearInfoList
        citeScoreYearInfo[]              … {@year, @status(Complete|In-Progress),
              citeScoreInformationList[].citeScoreInfo[].{citeScore, citeScoreSubjectRank[]}}
"""

import logging

from scopus_tools.scie import normalize_issn

logger = logging.getLogger(__name__)

# 1 リクエストに載せる ISSN 数。実測で 25 は正確に 25 件返ったが、
# 50 指定で 49 件、100 指定で 96 件と**黙って減る**。25 で固定する。
BATCH_SIZE = 25

# パーセンタイル → 四分位。JCR の quartile と並べて比較できるようにするため。
_QUARTILE_CUTOFFS = ((75, "Q1"), (50, "Q2"), (25, "Q3"))


def quartile_from_percentile(percentile):
    """分野内パーセンタイルを Q1〜Q4 に落とす。None は None。"""
    if percentile is None:
        return None
    for cutoff, label in _QUARTILE_CUTOFFS:
        if percentile >= cutoff:
            return label
    return "Q4"


def _num(value):
    """"21.9" / "2,825" / None → float / None。0 と「値なし」を混同しない。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(value):
    n = _num(value)
    return int(n) if n is not None else None


def _listify(value):
    """Elsevier の応答は 1 件のとき dict、複数のとき list になることがある。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _latest(entries):
    """[{"@year": "2025", "$": "2.838"}] を {year, value} に。最新年を採る。"""
    best = None
    for e in _listify(entries):
        year = _int(e.get("@year"))
        value = _num(e.get("$"))
        if year is None or value is None:
            continue
        if best is None or year > best["year"]:
            best = {"year": year, "value": value}
    return best


def parse_serial_entry(entry):
    """Serial Title の 1 誌分を平坦な dict にする。

    `citescore_by_year` を年 → 指標の辞書にしておくのが要点。出版年に合わせて
    選べることがこのモジュールの存在理由なので、最新年だけに畳んではいけない。
    """
    issns = [i for i in (normalize_issn(entry.get("prism:issn")),
                         normalize_issn(entry.get("prism:eIssn"))) if i]

    # 分野コードは数字なので、名前を引けるようにしておく
    subject_names = {}
    subjects = []
    for s in _listify(entry.get("subject-area")):
        code = str(s.get("@code") or "").strip()
        name = (s.get("$") or "").strip()
        if code:
            subject_names[code] = name
        if name:
            subjects.append({"code": code, "abbrev": s.get("@abbrev") or "", "name": name})

    by_year = {}
    info_list = (entry.get("citeScoreYearInfoList") or {})
    for y in _listify(info_list.get("citeScoreYearInfo")):
        year = _int(y.get("@year"))
        if year is None:
            continue
        infos = _listify(y.get("citeScoreInformationList"))
        ci = None
        for block in infos:
            for cand in _listify(block.get("citeScoreInfo")):
                # docType="all" が総合値。無ければ最初のものを使う。
                if cand.get("docType") == "all":
                    ci = cand
                    break
                ci = ci or cand
            if ci is not None and ci.get("docType") == "all":
                break
        if ci is None:
            continue
        ranks = []
        for r in _listify(ci.get("citeScoreSubjectRank")):
            code = str(r.get("subjectCode") or "").strip()
            pct = _int(r.get("percentile"))
            ranks.append({
                "code": code,
                "name": subject_names.get(code, ""),
                "rank": _int(r.get("rank")),
                "percentile": pct,
                "quartile": quartile_from_percentile(pct),
            })
        by_year[year] = {
            "citescore": _num(ci.get("citeScore")),
            "status": y.get("@status") or "",
            "scholarly_output": _int(ci.get("scholarlyOutput")),
            "percent_cited": _int(ci.get("percentCited")),
            "ranks": ranks,
        }

    return {
        "issns": issns,
        "title": entry.get("dc:title"),
        "publisher": entry.get("dc:publisher"),
        "aggregation_type": entry.get("prism:aggregationType") or "",
        "subject_areas": subjects,
        "citescore_by_year": by_year,
        # SJR / SNIP は年次履歴が返らない(最新年のみ)。出版年には合わせられないので
        # その事実を year 付きで持たせ、利用側が判断できるようにする。
        "sjr": _latest((entry.get("SJRList") or {}).get("SJR")),
        "snip": _latest((entry.get("SNIPList") or {}).get("SNIP")),
    }


def _best_rank(ranks):
    """複数分野のうち最良(パーセンタイル最大)を代表値にする。"""
    usable = [r for r in ranks or [] if r.get("percentile") is not None]
    return max(usable, key=lambda r: r["percentile"]) if usable else None


def pick_metrics_for_year(record, year=None):
    """出版年に対応する指標を選ぶ。**このモジュールの核**。

    どの年の値なのかを黙って隠さないこと。JCR を常用から外した理由が
    「出版年に合わせられない」ことなので、合わせた/合わせられなかったを必ず返す:

      year_match = "exact"   … その年の CiteScore がある
                   "nearest" … 無いので最も近い年で代用した(metric_year を見ること)
                   "none"    … その誌に CiteScore が 1 年も無い(値は None)

    `provisional` は暫定値(@status が In-Progress。最新年は集計途中)。
    """
    by_year = (record or {}).get("citescore_by_year") or {}
    result = {
        "citescore": None,
        "metric_year": None,
        "year_match": "none",
        "provisional": False,
        "percentile": None,
        "quartile": None,
        "rank": None,
        "subject": None,
        "ranks": [],
        "sjr": (record or {}).get("sjr"),
        "snip": (record or {}).get("snip"),
        "journal": (record or {}).get("title"),
        "aggregation_type": (record or {}).get("aggregation_type", ""),
    }
    if not by_year:
        return result

    years = sorted(by_year)
    if year and year in by_year:
        chosen, match = year, "exact"
    elif year:
        # 最も近い年。同着なら新しい方(収録前の論文で古すぎる値を当てないため)
        chosen = min(years, key=lambda y: (abs(y - year), -y))
        match = "nearest"
    else:
        chosen, match = years[-1], "nearest"

    m = by_year[chosen]
    best = _best_rank(m["ranks"])
    result.update({
        "citescore": m["citescore"],
        "metric_year": chosen,
        "year_match": match,
        "provisional": str(m.get("status", "")).lower().startswith("in-progress"),
        "percentile": best["percentile"] if best else None,
        "quartile": best["quartile"] if best else None,
        "rank": best["rank"] if best else None,
        "subject": best["name"] or best["code"] if best else None,
        "ranks": m["ranks"],
    })
    return result


def lookup(paper, table):
    """論文の issn / eissn で雑誌レコードを引く。無ければ None。"""
    for key in ("issn", "eissn"):
        issn = normalize_issn(paper.get(key))
        if issn and issn in table:
            return table[issn]
    return None


def annotate_papers_metrics(papers, table, use_publication_year=True):
    """各論文に `metrics` を付与する(in-place)。付いた件数を返す。

    既定では**その論文の出版年**の指標を選ぶ。`use_publication_year=False` にすると
    常に最新年を使う(「この雑誌の現在の水準」を見たい場合)。
    """
    if not table:
        return 0
    hits = 0
    for p in papers or []:
        rec = lookup(p, table)
        if rec is None:
            p["metrics"] = None
            continue
        year = p.get("year") if use_publication_year else None
        p["metrics"] = pick_metrics_for_year(rec, year or None)
        hits += 1
    return hits


def _median(values):
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _stats(values):
    return {
        "count": len(values),
        "median": round(_median(values), 3) if values else None,
        "max": round(max(values), 3) if values else None,
    }


def summarize_metrics(papers):
    """論文群の雑誌指標をまとめる。

    **主指標は作らない。** CiteScore・パーセンタイル・SJR・SNIP を対等に並べ、
    どれを重く見るかは読み手に委ねる。平均ではなく**中央値**を出すのは、
    雑誌指標の分布が強く歪むため(1 本の高 IF 誌が平均を動かす)。

    `year_match` の内訳は集計の信頼度そのもの。`nearest` が多ければ、
    出版年の指標として読んではいけない。
    """
    cs, pct, sjr, snip = [], [], [], []
    match = {"exact": 0, "nearest": 0, "none": 0}
    quartiles = {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0}
    provisional = 0
    without = 0

    for p in papers or []:
        m = p.get("metrics")
        if not m:
            without += 1
            match["none"] += 1
            continue
        match[m.get("year_match", "none")] = match.get(m.get("year_match", "none"), 0) + 1
        if m.get("citescore") is not None:
            cs.append(m["citescore"])
        if m.get("percentile") is not None:
            pct.append(float(m["percentile"]))
        if m.get("quartile") in quartiles:
            quartiles[m["quartile"]] += 1
        if (m.get("sjr") or {}).get("value") is not None:
            sjr.append(m["sjr"]["value"])
        if (m.get("snip") or {}).get("value") is not None:
            snip.append(m["snip"]["value"])
        if m.get("provisional"):
            provisional += 1

    return {
        "papers": len(papers or []),
        "without_metrics": without,
        "citescore": _stats(cs),
        "percentile": _stats(pct),
        "sjr": _stats(sjr),
        "snip": _stats(snip),
        "quartiles": quartiles,
        "year_match": match,
        "provisional_count": provisional,
        "note": ("CiteScore is not the Journal Impact Factor — it uses a 4-year window "
                 "and counts all document types, so it reads higher. Percentile is the "
                 "journal's rank within its Scopus subject area, which is the figure "
                 "that compares across fields. SJR and SNIP are latest-year only and "
                 "are not matched to the publication year."),
    }
