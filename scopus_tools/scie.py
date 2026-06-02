"""Web of Science 収録インデックス判定(SCIE / SSCI / AHCI / ESCI …)。

Scopus 自体には『どの WoS インデックスに収録されているか』を示すフィールドが無い
(これらは Clarivate の Web of Science 系の区分で、Scopus は別データベース)。
そこでユーザが用意したインデックス別の収録誌リスト(Clarivate Master Journal List の
エクスポート。インデックスごとに別 CSV)の ISSN を集合化し、各論文の ISSN / eISSN と
突き合わせて、該当する収録インデックス名を `wos_indexes`(リスト)として付与する。

インデックス名はファイル名の括弧内略号(例: "... (SCIE).csv" → "SCIE")から導出する。
外部リストは登録制で自動取得できないため、パスはユーザが明示的に渡す。
"""

import logging
import os
import re

import pandas as pd

logger = logging.getLogger(__name__)


def normalize_issn(value):
    """ISSN 文字列を比較用に正規化する。

    ハイフン・空白・その他の記号を除去し英数字のみの大文字 8 桁(末尾チェック桁 X 含む)に
    そろえる。"0028-0836" / "0028 0836" / "00280836" を同一視できるようにするのが目的。
    8 桁にならないものは不正値として None を返す。
    """
    if value is None:
        return None
    s = re.sub(r"[^0-9Xx]", "", str(value)).upper()
    return s if len(s) == 8 else None


def _split_issns(cell):
    """1 セルに複数 ISSN が入る場合(空白 / カンマ / セミコロン区切り)を分解する。"""
    if cell is None:
        return []
    return [tok for tok in re.split(r"[\s,;|]+", str(cell)) if tok]


def load_scie_issn_set(path):
    """SCIE 収録誌リストから正規化済み ISSN の集合を作る。

    CSV を想定し、列名に 'issn'(大文字小文字問わず、eISSN 等も含む)を含む列の値を
    すべて収集する。該当列が見つからない場合(ヘッダ無し等)は全列の値を対象にする。
    フォールバックとして、CSV として読めない単純なテキスト(1 行 1 ISSN)も受け付ける。
    """
    issns = set()
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        df = None

    if df is not None and len(df.columns) > 0:
        issn_cols = [c for c in df.columns if "issn" in str(c).lower()]
        cols = issn_cols or list(df.columns)
        for col in cols:
            for cell in df[col]:
                for tok in _split_issns(cell):
                    n = normalize_issn(tok)
                    if n:
                        issns.add(n)
        if issn_cols:
            logger.debug("SCIE list: matched ISSN columns=%s", issn_cols)
        else:
            # ISSN 列名が無い場合、ヘッダ自体が ISSN(ヘッダ無しファイルを
            # pandas が 1 行目をヘッダとして読んだ)可能性があるので列名も拾う。
            for col in df.columns:
                for tok in _split_issns(col):
                    n = normalize_issn(tok)
                    if n:
                        issns.add(n)
    else:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                for tok in _split_issns(line):
                    n = normalize_issn(tok)
                    if n:
                        issns.add(n)

    logger.info("Loaded %d unique SCIE ISSNs from %s", len(issns), path)
    return issns


def annotate_papers(papers, issn_set):
    """各論文に `is_scie`(bool)を付与し、SCIE 該当件数を返す。

    論文の `issn` または `eissn` のいずれかが集合に含まれれば SCIE 収録とみなす。
    papers は in-place で更新する。
    """
    matched = 0
    for p in papers:
        candidates = []
        for key in ("issn", "eissn"):
            candidates.extend(_split_issns(p.get(key)))
        hit = any(normalize_issn(tok) in issn_set for tok in candidates)
        p["is_scie"] = hit
        if hit:
            matched += 1
    return matched


def derive_index_label(path):
    """ファイル名から WoS インデックス名(略号)を導出する。

    Clarivate のエクスポート名は "... (SCIE).csv" のように末尾の括弧に略号を含むので
    それを採用する。括弧が無ければ拡張子を除いたファイル名を使う。
    """
    name = os.path.basename(path)
    m = re.search(r"\(([A-Za-z][A-Za-z0-9&/ +-]*?)\)", name)
    if m:
        return m.group(1).strip()
    return os.path.splitext(name)[0].strip() or name


def load_index_sets(paths):
    """複数のインデックス CSV を読み込み {インデックス名: ISSN集合} を返す。

    同名インデックスに導出される複数ファイルは和集合にマージする。
    """
    sets = {}
    for path in paths:
        label = derive_index_label(path)
        issns = load_scie_issn_set(path)
        sets[label] = sets.get(label, set()) | issns
        logger.info("Index '%s': %d ISSNs (from %s)", label, len(issns), path)
    return sets


def annotate_papers_indexes(papers, index_sets):
    """各論文に該当する収録インデックス名のリスト `wos_indexes` を付与する。

    index_sets は {インデックス名: 正規化済みISSN集合}。論文の issn / eissn のいずれかが
    その集合に含まれれば、そのインデックスに収録とみなす。後方互換のため `is_scie`
    (=`"SCIE" in wos_indexes`)も併せて設定する。1 つ以上のインデックスに該当した
    論文数を返す。papers は in-place で更新する。
    """
    matched = 0
    for p in papers:
        norm = set()
        for key in ("issn", "eissn"):
            for tok in _split_issns(p.get(key)):
                n = normalize_issn(tok)
                if n:
                    norm.add(n)
        labels = sorted(label for label, issns in index_sets.items() if norm & issns)
        p["wos_indexes"] = labels
        p["is_scie"] = "SCIE" in labels
        if labels:
            matched += 1
    return matched
