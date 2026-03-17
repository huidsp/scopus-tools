# scopus_tools

Scopus API を使って研究者情報と論文情報を取得し、集計・要約・AI 分析を行う Python ツールです。著者検索、年次集計、要約表示、バッチ処理、専門分野推定を提供します。

## 機能

- 著者名から Scopus ID を検索
- 複数 Scopus ID をまとめた論文検索と重複除去
- H-index / G-index の計算
- 総論文数、総引用数、最近 5 年の件数、筆頭著者件数の集計
- 人が読みやすい要約レポートの表示
- CSV 入力による一括検索と一括集計
- OpenAI を用いた研究専門性の推定

## 必要要件

- Python 3.9 以上
- Scopus API キー
- OpenAI API キー: `analyze` コマンドを使う場合のみ必要

Scopus API キーは Elsevier Developer Portal で取得します。

## インストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 環境変数

プロジェクトルートに `.env` を作成します。

```env
SCOPUS_API_KEY=your_scopus_api_key
OPENAI_API_KEY=your_openai_api_key
```

`OPENAI_API_KEY` が未設定でも、`analyze` 以外のコマンドは利用できます。

## CLI

エントリーポイントは `scopus-tools` です。

### `search`

著者名から Scopus ID を検索します。単体検索では結果を表示し、CSV 入力では所属機関ごとに ID をまとめた CSV を出力します。

単体検索:

```bash
scopus-tools search --name "Hiroyuki Okamura"
```

CSV 一括検索:

```bash
scopus-tools search --input authors.csv --output author_ids.csv
```

入力 CSV の例:

```csv
Name
Hiroyuki Okamura
Taro Tanaka
```

出力 CSV の主な列:

- `Name`
- `Scopus ID`
- `Affiliation`

### `stats`

年範囲を指定して、論文数、総引用数、論文タイプ別件数を CSV に出力します。

```bash
scopus-tools stats --year "[2020,2024]" --input author_ids.csv --output stats.csv
```

入力 CSV に必要な列:

- `Name`
- `Scopus ID`
- `Affiliation`（任意。ある場合はそのまま出力に引き継がれます）

出力 CSV には次のような列が含まれます。

- 入力元の列
- `paper_count`
- `total_citations`
- `Article`, `Review` などのタイプ別件数

### `summary`

単一または複数の Scopus ID を対象に、研究歴、引用指標、指定した年の集計、被引用数上位 5 件を表示します。

```bash
scopus-tools summary 12345678,87654321
```

年範囲を指定する例:

```bash
scopus-tools summary 12345678 --years "[2021,2025]"
```

### `batch`

CSV に含まれる複数著者について要約統計をまとめて出力します。

```bash
scopus-tools batch --input author_ids.csv --output summary.csv
```

出力 CSV の列:

- `Name`
- `Scopus IDs`
- `Affiliation`
- `Research Years`
- `Start Year`
- `Total Papers`
- `Total Citations`
- `Total First Author`
- `Recent 5Y Papers`
- `Recent 5Y Citations`
- `Recent 5Y First Author`
- `H-index`
- `G-index`

### `analyze`

論文タイトルをもとに OpenAI で研究専門性を推定します。

```bash
scopus-tools analyze 12345678,87654321
```

出力言語を指定する例:

```bash
scopus-tools analyze 12345678 --lang en
```

## Python から使う

```python
from scopus_tools.api import ScopusClient
from scopus_tools.core import summarize_papers
from scopus_tools.ai_engine import estimate_expertise

client = ScopusClient()

given, surname = client.get_author_profile("12345678")
papers = client.search_papers(["12345678", "87654321"])
report = summarize_papers(papers)

print(given, surname)
print(report)

analysis = estimate_expertise(papers, lang="ja")
print(analysis)
```

## プロジェクト構成

```text
scopus_tools/
	__init__.py
	ai_engine.py
	api.py
	cli.py
	core.py
	utils.py
pyproject.toml
```
