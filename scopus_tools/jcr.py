"""JCR (Journal Citation Reports) のエクスポート CSV を読み、論文に IF / 分位を付ける。

`scie.py` が「WoS のどの版に収録されているか」を扱うのに対し、こちらは
**雑誌の指標**(Journal Impact Factor、JIF 分位、JCI)を扱う。突き合わせは
どちらも ISSN なので仕組みは似ているが、`scie` は ISSN の**集合**、こちらは
ISSN → **値**の対応表になる。

CSV は jcr.clarivate.com の Journals 画面から手で書き出す(Master Journal List と
同じく登録制で、API では取れない — WoS Starter の `/journals` は JCR への
**リンク**しか返さず、指標の数値は別契約の Journals API が要る)。
**1 回のエクスポートは 600 誌まで**なので、カテゴリで絞って複数回に分けて落とす。

### 実物の CSV に合わせて気をつけていること(すべて実測に基づく)

1. **ヘッダは 3 行目**。1 行目は "Journal Data Filtered By: ..." のバナー、2 行目は空行。
2. **末尾 2 行は著作権表記**("Copyright (c) 2026 Clarivate" など)。ISSN を持たない
   行として落ちるが、明示的に弾く。
3. **`ISSN` 列の値が文字列 `"N/A"` のことがある**(eISSN しか無い雑誌)。これを
   そのままキーにすると全部が "N/A" という 1 つの雑誌に潰れる。`normalize_issn` が
   None を返すので実害は無いが、eISSN へのフォールバックが必須。
4. **JIF / JCI の列名に年が入る**(`2025 JIF`, `2025 JCI`)。年ごとに変わるので
   完全一致で探してはいけない。接尾辞で拾い、ついでに年を控えて出所を明示する。
5. **1 行 = 1 雑誌 × 1 カテゴリ**。同じ雑誌が複数カテゴリで繰り返し現れ、
   **分位はカテゴリごとに違いうる**(片方 Q1、片方 N/A など)。JIF は雑誌に 1 つ、
   分位はカテゴリごと、という構造をそのまま保持する。
6. 数値に桁区切りが入る(`"78,841"`)。`Edition` が `"SCIE, SSCI"` のこともある。
"""

import csv
import glob
import logging
import os
import re

from scopus_tools.scie import normalize_issn

logger = logging.getLogger(__name__)

# 値が無いことを表す表記。空文字と同じ扱いにする。
_NA = {"", "n/a", "na", "-", "n.a."}

_QUARTILES = ("Q1", "Q2", "Q3", "Q4")


def _clean(value):
    """セルの値を正規化する。N/A 系は None。"""
    if value is None:
        return None
    s = str(value).strip().strip('"').strip()
    return None if s.lower() in _NA else s


def _number(value):
    """桁区切り・パーセント記号を落として float に。無理なら None。"""
    s = _clean(value)
    if s is None:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    # "<0.1" のような表記が来ても落とさない
    s = s.lstrip("<>")
    try:
        return float(s)
    except ValueError:
        return None


def _find_header(lines):
    """ヘッダ行の位置を返す。見つからなければ None。

    1 行目のバナーを飛ばすために決め打ちで 3 行目を読むのではなく、
    `Journal name` と `ISSN` を含む行を探す — 書き出し設定でバナーの行数が
    変わっても壊れないようにするため。
    """
    for i, line in enumerate(lines[:20]):
        low = line.lower()
        if "journal name" in low and "issn" in low:
            return i
    return None


def parse_jcr_year(text):
    """バナーから "Selected JCR Year: 2025" の年を拾う。無ければ None。"""
    m = re.search(r"JCR\s+Year:\s*(\d{4})", str(text or ""))
    return int(m.group(1)) if m else None


def load_jcr_csv(path):
    """JCR の CSV を 1 つ読み、`{正規化ISSN: レコード}` を返す。

    レコードは
      {"journal", "jif", "jci", "quartile", "categories": [{category, quartile, edition}],
       "jcr_year", "issns"}
    で、`quartile` は全カテゴリ中の**最良**(Q1 が最良)。人事選考では
    「Q1 の雑誌か」を見たいことが多く、カテゴリごとの内訳は `categories` に残す。
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        lines = f.read().splitlines()
    if not lines:
        return {}

    header_at = _find_header(lines)
    if header_at is None:
        logger.warning("JCR CSV %s: header row not found; skipped", path)
        return {}
    jcr_year = parse_jcr_year(lines[0]) if header_at else None

    reader = csv.DictReader(lines[header_at:])
    fields = [c for c in (reader.fieldnames or []) if c]
    jif_col = next((c for c in fields if c.strip().upper().endswith("JIF")), None)
    jci_col = next((c for c in fields if c.strip().upper().endswith("JCI")), None)
    quart_col = next((c for c in fields if "quartile" in c.lower()), None)
    if jif_col is None:
        logger.warning("JCR CSV %s: no JIF column in %s; skipped", path, fields)
        return {}
    if jcr_year is None:
        # 列名 "2025 JIF" からも年を拾えるので、バナーが無くても諦めない
        m = re.match(r"\s*(\d{4})", jif_col)
        jcr_year = int(m.group(1)) if m else None

    table = {}
    for row in reader:
        journal = _clean(row.get("Journal name"))
        if not journal or journal.lower().startswith(("copyright", "by exporting")):
            continue                                   # 末尾の著作権表記
        # ISSN 列は "N/A" のことがある。eISSN に必ずフォールバックする。
        issns = [i for i in (normalize_issn(_clean(row.get("ISSN"))),
                             normalize_issn(_clean(row.get("eISSN")))) if i]
        if not issns:
            continue
        category = _clean(row.get("Category"))
        quartile = _clean(row.get(quart_col)) if quart_col else None
        entry = {
            "journal": journal,
            "jif": _number(row.get(jif_col)),
            "jci": _number(row.get(jci_col)) if jci_col else None,
            "jcr_year": jcr_year,
            "issns": issns,
        }
        for issn in issns:
            rec = table.setdefault(issn, {**entry, "categories": [], "quartile": None})
            if category:
                rec["categories"].append({
                    "category": category,
                    "quartile": quartile,
                    "edition": _clean(row.get("Edition")),
                })
            # 分位はカテゴリごとに違う。最良を代表値にする。
            for q in _QUARTILES:
                if quartile == q:
                    if rec["quartile"] is None or q < rec["quartile"]:
                        rec["quartile"] = q
                    break
    logger.info("JCR: %s から %d 誌 (JCR %s)", os.path.basename(path),
                len({id(v) for v in table.values()}), jcr_year)
    return table


def load_jcr_tables(paths):
    """複数の JCR CSV を読み、1 つの対応表にまとめる。

    同じ ISSN が複数ファイルに現れたら、**カテゴリを統合**する(カテゴリ別に
    書き出すと 1 誌が複数ファイルにまたがるため)。JIF は雑誌に 1 つなので
    先勝ちでよいが、食い違ったら警告する — 別の JCR 年のファイルが
    混ざっている合図なので、黙って混ぜてはいけない。
    """
    merged = {}
    for path in paths or []:
        try:
            table = load_jcr_csv(path)
        except (OSError, csv.Error) as e:
            logger.warning("JCR CSV %s could not be read: %s", path, e)
            continue
        for issn, rec in table.items():
            cur = merged.get(issn)
            if cur is None:
                merged[issn] = rec
                continue
            if (cur.get("jcr_year") and rec.get("jcr_year")
                    and cur["jcr_year"] != rec["jcr_year"]):
                logger.warning("JCR: %s mixes JCR %s and %s — using %s",
                               issn, cur["jcr_year"], rec["jcr_year"], cur["jcr_year"])
            seen = {(c["category"], c.get("edition")) for c in cur["categories"]}
            for c in rec["categories"]:
                if (c["category"], c.get("edition")) not in seen:
                    cur["categories"].append(c)
            for q in _QUARTILES:
                if rec["quartile"] == q:
                    if cur["quartile"] is None or q < cur["quartile"]:
                        cur["quartile"] = q
                    break
    return merged


def resolve_jcr_paths(jcr_list=None, jcr_dir=None):
    """JCR CSV のパス群を解決する(`scie.resolve_index_paths` と同じ優先順位)。

      1. jcr_list … 明示されたパス
      2. jcr_dir  … そのディレクトリの `*.csv`
      3. 自動検出 … カレントの `JCR_*.csv` と `jcr/*.csv`
    """
    if jcr_list:
        paths = list(jcr_list)
    elif jcr_dir:
        paths = sorted(glob.glob(os.path.join(jcr_dir, "*.csv")))
    else:
        paths = (sorted(glob.glob("JCR_*.csv"))
                 + sorted(glob.glob(os.path.join("jcr", "*.csv"))))
    return list(dict.fromkeys(paths))


def discover_jcr_table(jcr_list=None, jcr_dir=None):
    """起動時ロードの入口。見つけた CSV をすべて読んで 1 つの表にする。"""
    paths = resolve_jcr_paths(jcr_list=jcr_list, jcr_dir=jcr_dir)
    return load_jcr_tables(paths) if paths else {}


def lookup(paper, table):
    """論文の issn / eissn で JCR レコードを引く。無ければ None。"""
    for key in ("issn", "eissn"):
        issn = normalize_issn(paper.get(key))
        if issn and issn in table:
            return table[issn]
    return None


def annotate_papers_jcr(papers, table):
    """各論文に `jcr` を付与する(in-place)。付いた件数を返す。

    付ける値は雑誌の指標であって論文の被引用数ではない。**IF は雑誌の指標で
    あり個々の論文の質を表さない**ので、キー名を `jcr` と明示して混同を避ける。
    """
    if not table:
        return 0
    hits = 0
    for p in papers or []:
        rec = lookup(p, table)
        if rec is None:
            p["jcr"] = None
            continue
        p["jcr"] = {
            "jif": rec["jif"],
            "quartile": rec["quartile"],
            "jci": rec["jci"],
            "jcr_year": rec["jcr_year"],
            "categories": [c["category"] for c in rec["categories"]],
        }
        hits += 1
    return hits


def summarize_jcr(papers):
    """論文群の JCR 指標をまとめる(分位別件数、IF の合計・中央値)。

    平均ではなく**中央値**も返すのは、IF が極端に歪んだ分布を持つため
    (1 本の Nature 系で平均が跳ね上がる)。
    """
    vals = [p["jcr"]["jif"] for p in papers or []
            if p.get("jcr") and p["jcr"].get("jif") is not None]
    quarts = {q: 0 for q in _QUARTILES}
    unknown = 0
    for p in papers or []:
        q = (p.get("jcr") or {}).get("quartile")
        if q in quarts:
            quarts[q] += 1
        else:
            unknown += 1
    vals_sorted = sorted(vals)
    median = None
    if vals_sorted:
        mid = len(vals_sorted) // 2
        median = (vals_sorted[mid] if len(vals_sorted) % 2
                  else (vals_sorted[mid - 1] + vals_sorted[mid]) / 2)
    return {
        "with_jif": len(vals),
        "without_jif": len(papers or []) - len(vals),
        "jif_sum": round(sum(vals), 3) if vals else 0.0,
        "jif_median": round(median, 3) if median is not None else None,
        "jif_max": max(vals) if vals else None,
        "quartiles": quarts,
        "quartile_unknown": unknown,
        "note": ("JIF is a journal-level metric and does not measure an individual "
                 "paper. Quartile is the best across the journal's categories."),
    }
