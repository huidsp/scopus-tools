# scopus_tools

Elsevier Scopus API と 科研費 (KAKEN) API から研究者の業績データを取得し、
書誌指標を計算するツールキットです。依存は `requests` と `python-dotenv` だけで、
インストールは CLI のみ **16MB**、MCP 込みでも **54MB** です。

フロントエンドは 2 つ:

- **CLI** (`scopus-tools`) — CSV 入出力を含むバッチ処理向け
- **MCP サーバ** (`scopus-tools mcp`) — Claude Code / Claude Desktop などの
  MCP クライアントから、データを直接引くため

> **このパッケージは API 経由で LLM を呼びません。**
> 業績評価・研究分野の推定・研究者の比較といった判断は、MCP ホスト側のモデルが
> ここのツールを対話的に呼んで自分で行います。パッケージ側に LLM 呼び出しを持つと
> 入れ子構造になり、コスト・レイテンシ・キー管理が二重になるためです。

## 目次

- [機能](#機能)
- [クイックスタート](#クイックスタート)
- [必要要件](#必要要件)
- [インストール](#インストール)
- [MCP サーバ](#mcp-サーバ)
- [API キーの取得と設定](#api-キーの取得と設定)
  - [Scopus API キー (`SCOPUS_API_KEY`)](#scopus-api-キー-scopus_api_key)
  - [CiNii アプリケーション ID (`KAKEN_APP_ID`)](#cinii-アプリケーション-id-kaken_app_id)
  - [`.env` ファイルの作成](#env-ファイルの作成)
- [年範囲の指定](#年範囲の指定)
- [CLI リファレンス](#cli-リファレンス)
- [キャッシュとクォータ](#キャッシュとクォータ)
- [Web of Science 収録インデックス](#web-of-science-収録インデックス)
- [Docker での利用](#docker-での利用)
- [Python API](#python-api)
- [プロジェクト構成](#プロジェクト構成)
- [トラブルシューティング](#トラブルシューティング)

---

## 機能

- 著者名から Scopus ID を検索(同名異人・複数 ID 分裂への対応込み)
- 複数 Scopus ID をまとめた論文検索・重複除去
- H-index / G-index の計算、年範囲別の論文数・被引用数・筆頭著者数の集計
- 論文ごとの著者順位(`2/3`、`1/4 (first)`)
- Web of Science 収録インデックス(SCIE / SSCI / AHCI / ESCI)の ISSN 突き合わせ
- 科研費 (KAKEN) の研究者検索・獲得課題サマリ、氏名からの研究者番号マッチング
- 人が読みやすい要約レポート(テキスト / JSON / CSV)
- CSV 入力による一括検索・一括集計
- MCP 経由でのデータ取得と、プロジェクト単位(**プロジェクト → 複数研究者**)の結果永続化
- SQLite によるレスポンスキャッシュ(Scopus のクォータ節約)、レート制限の自動制御、
  取得日のずれの警告

---

## クイックスタート

```bash
# 1. クローンと仮想環境
git clone https://github.com/huidsp/scopus-tools.git
cd scopus-tools
python3.12 -m venv .venv && source .venv/bin/activate

# 2. 依存関係(MCP 込み)
pip install -e ".[mcp]"

# 3. API キーを .env に書く(下のセクション参照)

# 4. 動作確認
scopus-tools search --name "Hiroyuki Okamura"

# 5. Claude Code に MCP サーバとして登録(絶対パスと鍵の受け渡しは自動)
scopus-tools mcp-setup --scope user --scie-dir "$PWD/index"
```

---

## 必要要件

- Python 3.12 以上
- API キー: `SCOPUS_API_KEY`(必須)、`KAKEN_APP_ID`(科研費機能を使う場合)
- 依存は `requests` と `python-dotenv` のみ(CLI で 16MB、MCP 込みで 54MB)

---

## インストール

### 開発インストール(推奨)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[mcp,dev]"
```

オプション extras:
- `[mcp]` — MCP サーバ用(`scopus-tools mcp` を使うなら必須)
- `[dev]` — `pytest`(テスト実行用)

### 直接インストール(CLI だけ使う場合)

```bash
pip install "git+https://github.com/huidsp/scopus-tools.git"
```

---

## MCP サーバ

`scopus-tools mcp` は MCP (Model Context Protocol) サーバとして stdio で待ち受けます。
CLI と同じ内部ロジックを、ホスト側モデルから呼べる形で公開したものです。

### 提供ツール

**データ取得**

| ツール | 引数 | 内容 |
|---|---|---|
| `search_author` | `first_name`, `last_name` | Scopus 著者候補を検索。姓名を分けて渡す(1 リクエスト)。0 件なら入れ替えて再試行 |
| `author_profile` | `author_id` | Scopus 著者 ID → 姓名 |
| `author_summary` | `author_ids`, `year_range` | H/G-index、被引用、筆頭著者数、WoS 収録数などの集計 |
| `list_papers` | `author_ids`, `year_range`, `limit`, `scie_only` | 期間内論文の一覧(`author_position` / `author_count` / `wos_indexes` 付き) |
| `kaken_search_researcher` | `name` | KAKEN(NRID)研究者候補の検索 |
| `kaken_grants` | `researcher_id`, `role` | 研究者番号から科研費課題一覧 |
| `link_kaken_researcher` | `first_name`, `last_name`, `auto` | Scopus 氏名 → KAKEN 研究者番号の自動照合 |
| `cache_stats` | — | キャッシュの状態と Elsevier クォータの残量 |

取得系ツールはすべて `refresh`(既定 false)を受け、応答に `as_of` / `as_of_note`
(いつ時点のデータか)が入ります。

**プロジェクト永続化**(複数研究者を蓄積して比較する用途)

| ツール | 引数 | 内容 |
|---|---|---|
| `list_projects` | — | 保存済みプロジェクト一覧 |
| `read_project` | `name` | 研究者一覧と比較結果を含む全体を取得 |
| `create_project` | `name` | 空のプロジェクトを作成 |
| `delete_project` | `name` | 削除 |
| `save_researcher_section` | `project`, `researcher`, `section`, `data` | 研究者の `scopus`/`kaken`/`ai` セクションを merge 保存 |
| `save_comparison` | `project`, `table_md`, `ai_evaluation`, `selected_names` | 横断比較の結果を保存 |

### 使い方のメモ

- `author_ids` は `"123,456"` でもリスト `["123","456"]` でも受理します。
- `year_range` は CLI の `--years` と同じ書式(`2021-2025` / `[2021,2025]` など)。
  省略時は **前年を含む直近 5 年**。
- `list_papers` はトークン量を抑えるため既定 200 件で打ち切り、切り捨て時は
  `truncated: true` と `total_count` を返します。
- API キーが未設定でもサーバは起動でき、該当ツールの呼び出し時に
  `{"error": "SCOPUS_API_KEY is not set"}` を返します。
- `link_kaken_researcher` は候補が複数あると(`auto=False` のとき)空を返します。
  その場合はモデルが `kaken_search_researcher` で候補を確認して選ぶ想定です。
- 取得が Scopus 側の都合で完結しなかった場合(ページング上限、通信エラーなど)、
  応答に `incomplete: true` と理由が入ります。これは `truncated`(こちらが `limit` で
  切った)とは別物で、**業績の全体像として扱ってはいけない**という意味です。

### 登録(インストール)

**MCP サーバ用に別途インストールするものはありません。** `pip` で入れた
`scopus-tools` がそのまま MCP サーバになります(`scopus-tools mcp`)。
「インストール」にあたるのは **MCP クライアント側に起動コマンドを登録すること**だけです。

登録する前に、次の 2 点を押さえてください。**ここを外すとほぼ確実に動きません。**

1. **絶対パスで指定する。** MCP クライアントはあなたのシェルを経由せずにプロセスを
   起動するので、venv の有効化も `PATH` も引き継がれません。`scopus-tools` とだけ
   書いても見つかりません
2. **API キーは登録時に明示的に渡す。** 同じ理由でシェルの `export` は届きません。
   `.env` は「実行時のカレントディレクトリ」または「パッケージの位置」から探されますが、
   MCP クライアントが設定するカレントディレクトリは環境によって違うので、
   **登録時に環境変数として渡すのが確実**です

> **macOS で最重要**: `~/Documents` / `~/Desktop` / `~/Downloads` は TCC
> (プライバシー保護)の対象で、**Claude Desktop はそこを読めません**。リポジトリの
> `.venv` を直接登録すると `pyvenv.cfg` すら読めず、Python が起動する前に落ちます:
>
> ```
> PermissionError: [Errno 1] Operation not permitted: '.../.venv/pyvenv.cfg'
> ```
>
> ターミナル経由の Claude Code は権限があるため通ってしまい、気付きにくい罠です。
> **実行ファイルも索引 CSV も TCC 保護外に置いてください。**
> `mcp-setup` はこれを検出して登録を拒否します。

#### 方法 A: `mcp-setup` に任せる(推奨)

上の 3 点(絶対パス・鍵の受け渡し・TCC 保護パスの回避)を自動でやります。

```bash
# TCC 保護外にインストールする(~/.local は Claude Desktop の PATH にも入っている)
uv tool install "scopus_tools[mcp] @ /path/to/scopus-tools"

# 索引 CSV も TCC 保護外へ
mkdir -p ~/.scopus-tools/index && cp /path/to/scopus-tools/index/*.csv ~/.scopus-tools/index/

scopus-tools mcp-setup --scope user --scie-dir ~/.scopus-tools/index
scopus-tools mcp-setup --claude-desktop --scie-dir ~/.scopus-tools/index
```

自分の絶対パスを解決し、`.env` や環境変数から API キーを読んで登録します。
**鍵をどのファイルに平文で書いたかは実行時に必ず表示されます。**

```bash
scopus-tools mcp-setup --print              # 何も書かずに内容を確認(鍵は伏せて表示)
scopus-tools mcp-setup --claude-desktop     # Claude Desktop の設定に書く
scopus-tools mcp-setup --status             # 現在の登録を確認
scopus-tools mcp-setup --remove             # 登録を解除
scopus-tools mcp-setup --no-keys            # 鍵を埋め込まない
scopus-tools mcp-setup --fix-permissions    # .env が 644 なら 600 に直す
```

`--scope` の既定は `local`(そのディレクトリのみ)です。**どこからでも使うなら
`--scope user`** を付けてください。

Claude Desktop の設定を書き換えるときは `.bak` を取り、**書いた後に読み直して
既存の設定が 1 つも失われていないことを検証**します(失われていたら書き戻して中止)。

#### 方法 B: 手動で登録する

```bash
which scopus-tools        # 例: /Users/you/.local/bin/scopus-tools ← このパスを使う

claude mcp add scopus \
  -e SCOPUS_API_KEY=your_key \
  -e KAKEN_APP_ID=your_appid \
  -- "$HOME/.local/bin/scopus-tools" mcp \
     --scie-dir /path/to/index --projects-dir /path/to/projects
```

開発用の venv をそのまま使う場合:

```bash
claude mcp add scopus -- "$PWD/.venv/bin/scopus-tools" mcp --scie-dir "$PWD/index"
```

editable インストール(`pip install -e`)の場合に限り、`.env` はリポジトリ直下から
自動的に読まれるので `-e` は省略できます。

#### 方法 C: Docker

```bash
claude mcp add scopus -- docker run -i --rm --env-file /path/to/.env \
  -v /path/to/index:/data/index:ro \
  -v /path/to/projects:/data/projects \
  -v scopus-cache:/data/cache \
  ghcr.io/huidsp/scopus-tools:latest mcp --scie-dir /data/index
```

`-i` が必須です(MCP は stdin/stdout でやり取りするため)。

#### 登録スコープ

`claude mcp add` の既定は `--scope local`(そのディレクトリでのみ有効)です。
どこからでも使いたい場合は `--scope user` を付けてください。

#### Claude Desktop の場合

`claude_desktop_config.json` に直接書きます:

```json
{
  "mcpServers": {
    "scopus": {
      "command": "/Users/you/.local/bin/scopus-tools",
      "args": ["mcp", "--scie-dir", "/path/to/index"],
      "env": {
        "SCOPUS_API_KEY": "...",
        "KAKEN_APP_ID": "..."
      }
    }
  }
}
```

#### 確認

```bash
claude mcp list           # scopus が Connected になっていること
```

Claude Code から `/mcp` でツール一覧(14 個)が見えれば成功です。
プロジェクト JSON の既定の保存先は `~/.scopus-tools/projects/` です。

> stdout は MCP プロトコル専用です。ログと進捗はすべて stderr に出るため、
> 独自にツールを足す場合も `print()` で stdout に書かないでください。

---

## API キーの取得と設定

### Scopus API キー (`SCOPUS_API_KEY`)

論文・著者検索に必須(`kaken-search` / `kaken-summary` 以外の全コマンドで必要)。

1. [Elsevier Developer Portal](https://dev.elsevier.com/) にアクセスしてアカウント登録
2. **My API Key** から新規キーを発行
3. 利用機関の IP からのアクセスが必要(通常は大学等の機関ネットワーク経由)
4. 取得した英数字キーを `SCOPUS_API_KEY` として `.env` に保存

**注意**: Scopus API は機関契約に紐付くため、契約外のネットワークからは認証失敗します。
学外利用には機関 VPN や Institutional Token などの設定が別途必要な場合があります。

### CiNii アプリケーション ID (`KAKEN_APP_ID`)

科研費(KAKEN)関連機能を使う場合のみ必要
(`kaken-search` / `kaken-summary` / MCP の KAKEN 系ツール)。

1. [CiNii API 開発者向けページ](https://support.nii.ac.jp/ja/cinii/api/developer) に従って利用申請
2. 申請承認後、CiNii から発行される appid を `KAKEN_APP_ID` として `.env` に保存

KAKEN API は無料ですが、レートリミットがあるので大量取得時は注意。

### `.env` ファイルの作成

プロジェクトのルートに `.env` を作成します(`python-dotenv` が起動時に読み込みます):

```env
# 必須: Scopus API キー
SCOPUS_API_KEY=your_scopus_api_key

# 科研費連携用(任意)
KAKEN_APP_ID=your_cinii_application_id
```

**セキュリティ注意**:
- **`chmod 600 .env`** で自分だけが読める権限にすること
  (既定の 644 だと同じマシンの他ユーザに鍵が読まれます。
  `scopus-tools mcp-setup --fix-permissions` でも直せます)
- `.env` は **絶対に Git にコミットしないこと**(`.gitignore` に登録済み)
- 共有マシンや CI では環境変数として直接設定するか、Vault / Secrets Manager を使う
- キーが漏洩したら即座にプロバイダ側で無効化・再発行

CLI 起動時に `.env` が無くてもシェルの環境変数(`export SCOPUS_API_KEY=...`)があれば動作します。

### どのキーがどのコマンドで必要か

| コマンド | SCOPUS | KAKEN |
|---|:-:|:-:|
| `search`, `stats`, `summary`, `papers`, `batch` | 必須 | — |
| `kaken-search`, `kaken-summary` | — | 必須 |
| `mcp` | (起動時は不要) | (KAKEN ツールを使う時に必要) |

CLI は起動時に必須キーを自動チェックし、未設定なら即座にエラーで終了します。
MCP サーバはキーが無くても起動し、該当ツールの呼び出し時にエラーを返します。

---

## 年範囲の指定

`--years`(`stats` の `--year`)は次のいずれの書式でも受理します:

```text
2021-2025
2021,2025
2021:2025
[2021,2025]
```

`summary` / `papers` / `batch` で `--years` を省略した場合は **前年を含む直近 5 年** が
使われます(例: 2026 年実行 → 2021–2025)。進行中の今年を含めるとデータが半年ぶんしか
揃わないため、既定では除外しています。

---

## CLI リファレンス

エントリーポイントは `scopus-tools` です(`pip install` 後)。
または `python -m scopus_tools.cli` でも同じ。

### `search` — 著者名から Scopus ID を検索

```bash
# 姓名を明示(推奨。1 リクエストで済む)
scopus-tools search --first Hiroyuki --last Okamura

# 自由入力(空白で分割し「名 姓」の順と仮定。仮定した旨を stderr に表示)
scopus-tools search --name "Hiroyuki Okamura"

# 順序が本当に不明なときだけ(クォータを 2 倍消費する)
scopus-tools search --name "Hiroyuki Okamura" --try-both

# CSV 一括検索
scopus-tools search --input authors.csv --output author_ids.csv
```

入力 CSV は `First Name` / `Last Name` 列があればそれを使い(取り違えが無く 1 リクエスト)、
`Name` 列しか無ければ空白で分割して「名 姓」の順と仮定します。
出力 CSV: `Name`, `Scopus ID`, `Affiliation` の 3 列。

> **クォータ注**: Author Search は **週 5,000 件・2 req/秒** と Elsevier の中で最も厳しい枠です。
> 以前は姓名の順序を当てるため 1 検索で 2 リクエスト投げていましたが、現在は 1 リクエストです。
> 外した場合は姓名を入れ替えて呼び直してください(MCP ではモデルが自動で判断します)。

### `stats` — 年範囲ごとの集計

```bash
scopus-tools stats --years 2020-2024 --input author_ids.csv --output stats.csv
```

入力 CSV: `Name`, `Scopus ID`, (任意で `Affiliation`)。
出力に論文タイプ別件数(Article / Review 等)も含む。

### `summary` — 人間可読の業績サマリ

```bash
scopus-tools summary 12345678,87654321
scopus-tools summary 12345678 --years 2021-2025
scopus-tools summary --input author_ids.csv --output reports.txt  # CSV 一括処理
scopus-tools summary 12345678 --format json --output summary.json  # JSON 出力
```

研究歴 / 引用指標 / 評価期間集計 / 被引用上位 5 件を出力。

### `papers` — 期間内の論文一覧

```bash
scopus-tools papers 12345678 --years 2021-2025
scopus-tools papers 12345678 --format csv --output papers.csv
scopus-tools papers 12345678 --scie-list "index/*.csv" --scie-only
```

各論文に著者順位(`2/3`、`1/4 (first)`)が付きます。
`--scie-list` を渡すと WoS 収録インデックス名も表示され、`--scie-only` で
いずれかのインデックス収録誌だけに絞れます。

### `batch` — CSV 入出力の一括サマリ

```bash
scopus-tools batch --input author_ids.csv --output summary.csv
scopus-tools batch --input author_ids.csv --output summary.csv --years 2021-2025
```

出力 CSV: Name / Scopus IDs / Affiliation / Research Years / Start Year / Total Papers /
Total Citations / Total First Author / Recent 5Y Papers / Recent 5Y Citations /
Recent 5Y First Author / H-index / G-index。

### `kaken-search` — KAKEN 研究者検索

```bash
scopus-tools kaken-search --name "Hiroyuki Okamura"
scopus-tools kaken-search --id 80401243
```

### `kaken-summary` — KAKEN 獲得課題サマリ

```bash
scopus-tools kaken-summary 80401243
scopus-tools kaken-summary 80401243,12345678 --role principal_investigator
scopus-tools kaken-summary 80401243 --format json --output grants.json
```

役割別・種目別の件数、配分額合計、課題一覧を表示。
`--role principal_investigator` で代表者のみに絞れます。

### `mcp-setup` — MCP クライアントへの登録

```bash
scopus-tools mcp-setup --scope user --scie-dir ./index   # Claude Code に登録
scopus-tools mcp-setup --claude-desktop                  # Claude Desktop に登録
scopus-tools mcp-setup --print                           # 書かずに内容だけ確認
scopus-tools mcp-setup --status / --remove
```

詳細は [MCP サーバ セクション](#mcp-サーバ) を参照。

### `mcp` — MCP サーバ起動(stdio)

```bash
scopus-tools mcp                                       # 通常は MCP クライアントから起動される
scopus-tools mcp --scie-dir ./index                    # WoS インデックス CSV を読み込む
scopus-tools mcp --projects-dir ./projects             # プロジェクト JSON の保存先
```

詳細は [MCP サーバ セクション](#mcp-サーバ) を参照。

---

## キャッシュとクォータ

Scopus API のクォータは実際に厳しく、特に **Author Search は週 5,000 件 / 2 req 秒** です
(Scopus Search は週 20,000 件 / 9 req 秒、Author Retrieval は週 5,000 件 / 3 req 秒。
クォータは 7 日ごとにリセット)。そこで成功したレスポンスを SQLite に保存し、
同じ取得を繰り返してもクォータを消費しないようにしています。

- 保存先: `~/.scopus-tools/cache.sqlite3`
  (`--cache-db` / `$SCOPUS_TOOLS_CACHE_DB` で変更、`$SCOPUS_TOOLS_CACHE_DISABLE=1` で無効化)
- **API キーはキャッシュに保存されません。** キャッシュキーからも保存パラメータからも
  除外され、送信直前に付与されます
- 保存するのは **成功(200)レスポンスのみ**。失敗はスキーマ制約でも弾いています

### 有効期限は無く、古くなったら警告する

複数の研究者を比較するとき、**取得年月日がそろっている必要がある**ためです。
被引用数は時間とともに増えるので、6 月に取った人と 8 月に取った人を同じ表に並べると
後者が不当に有利になります。勝手に一部だけ再取得されると比較が壊れるので、
**自動失効はさせず、再取得は明示的に**(`--refresh`)行います。

古いデータは警告するだけです。しきい値は **API 種別ごと**:

| 種別 | 既定 | 理由 |
|---|---:|---|
| `scopus_search` | 7 日 | 被引用数・論文数が動く。評価の中核なので短く |
| `scopus_author_search` / `scopus_author_retrieval` | 90 日 | 名前 → ID の対応はほぼ変わらない |
| `kaken_project` | 30 日 | 採択課題は年度単位で増える |
| `kaken_researcher` | 90 日 | 研究者番号の対応はほぼ変わらない |

```bash
scopus-tools --stale-days-for scopus_search=14 summary 12345678   # 種別ごとに変更
scopus-tools --stale-days 30 summary 12345678                     # 全体の既定を変更
export SCOPUS_TOOLS_STALE_DAYS="scopus_search=14,kaken_project=60"
```

複数研究者を扱うとき、取得日が 1 日以上ばらついていると警告が出ます(stderr):

```text
[as-of warning] The compared data was not fetched on the same date (spread 46.3 days).
Citation counts are not comparable.
    Okamura: 2026-07-01 (46 days old)
    Dohi: 2026-08-16 (0 days old)
  Re-fetch the older entries with --refresh so every figure is as of the same date.
```

MCP でも同じ情報が `as_of` / `as_of_note` として全取得ツールの応答に入り、
`read_project` / `save_comparison` は取得日がそろっていなければ `as_of_warning` を返します
(**警告のみで、保存は拒否しません**)。

### レート制限

- API 種別ごとに秒間リクエスト数を守り、`X-RateLimit-*` ヘッダで残量を記録します
- **クォータ枯渇(429 + `X-ELS-Status: QUOTA_EXCEEDED`)はリトライしません。**
  リセット時刻を記録して即座に分かりやすいエラーにします(数日待つ sleep はしません)。
  以降そのファミリはリセットまで通信せずに失敗します
- 秒間スロットル超過の 429 と 5xx / タイムアウトは指数バックオフでリトライします
- 全リクエストにタイムアウト(接続 10 秒 / 読み取り 60 秒、`--timeout` で変更可)

### 運用コマンド

```bash
scopus-tools cache stats                 # 件数・容量・API 別・クォータ残量
scopus-tools cache list --older-than 30  # 古いエントリを一覧
scopus-tools cache clear --api scopus_search
scopus-tools cache vacuum
scopus-tools cache path

scopus-tools --refresh summary 12345678  # キャッシュを無視して取り直す
scopus-tools --offline summary 12345678  # キャッシュのみ(通信しない)
scopus-tools --no-cache summary 12345678 # キャッシュを読み書きしない
```

---

## Web of Science 収録インデックス

Scopus には「どの WoS インデックスに収録されているか」を示すフィールドがありません
(SCIE/SSCI/AHCI/ESCI は Clarivate の Web of Science 側の区分で、Scopus は別データベース)。
そこで **ユーザが用意したインデックス別の収録誌リスト**(Clarivate Master Journal List の
エクスポート)の ISSN と、各論文の ISSN / eISSN を突き合わせます。

- リストは登録制で自動ダウンロードできません。インデックスごとに 1 CSV をダウンロードしてください
  (1 つの CSV に複数インデックスの区別は入っていません)。
- インデックス名はファイル名の括弧内略号から導出します(`... (SCIE).csv` → `SCIE`)。
- CLI では `--scie-list CSV [CSV ...]`、MCP / Docker では `--scie-dir DIR`
  (その中の `*.csv` を全部読む)で渡します。
- どちらも指定しない場合は、起動ディレクトリの `*Citation Index*.csv` と
  `index/*.csv` を自動検出します。
- データファイルは `.gitignore` 済みです。

---

## Docker での利用

ローカルに Python 環境を作らず Docker だけで実行できます。
GitHub Container Registry (GHCR) で公式イメージを配布しています
(`linux/amd64` + `linux/arm64` マルチアーキ)。

### GHCR から pull

```bash
docker pull ghcr.io/huidsp/scopus-tools:latest   # 最新リリース
docker pull ghcr.io/huidsp/scopus-tools:main     # main 追随版
docker pull ghcr.io/huidsp/scopus-tools:0.5.0    # バージョン固定
```

### MCP サーバとして

MCP は stdio なので常駐サービスにはなりません。`-i` を付けて MCP クライアントから起動します:

```bash
docker run -i --rm --env-file .env \
  -v "$PWD/index:/data/index:ro" \
  -v "$PWD/projects:/data/projects" \
  -v scopus-cache:/data/cache \
  ghcr.io/huidsp/scopus-tools:latest \
  mcp --scie-dir /data/index --projects-dir /data/projects
```

### CLI として

```bash
docker run --rm --env-file .env ghcr.io/huidsp/scopus-tools:latest \
  search --name "Hiroyuki Okamura"

# CSV を読み書きする場合は作業ディレクトリをマウント
docker run --rm --env-file .env -v "$PWD:/work" -w /work \
  ghcr.io/huidsp/scopus-tools:latest \
  batch --input author_ids.csv --output summary.csv
```

### 注意事項

- **Scopus は機関 IP 認証**。Docker ホスト自体が機関ネットワーク(または機関 VPN 接続済み)で
  ないと、コンテナからも認証が通りません。
- `.env` は **イメージに焼き込まないでください**(`.dockerignore` で除外済)。
  必ず `--env-file` で外から渡す運用に。
- コンテナ内ユーザは UID 1000 (`appuser`)。ホスト側ディレクトリの所有者が違う場合は
  `chown -R 1000:1000 ./projects` するか `docker run --user "$(id -u):$(id -g)"` で揃えてください。

### メンテナ向け: イメージ公開フロー

`.github/workflows/docker-publish.yml` が以下のタイミングで自動ビルド & GHCR push します:

| トリガー | publish されるタグ |
|---|---|
| `main` への push | `:main`, `:sha-<short>` |
| `v*` タグの push (例: `v0.5.0`) | `:0.5.0`, `:0.5`, `:latest` |
| Pull Request | (ビルドのみ、push なし) |

```bash
git tag v0.5.0
git push origin v0.5.0
```

---

## Python API

```python
from scopus_tools.api import ScopusClient
from scopus_tools.core import summarize_papers, default_eval_year_range
from scopus_tools import scie

client = ScopusClient()  # SCOPUS_API_KEY を env から読む

given, surname = client.get_author_profile("12345678")
papers = client.search_papers(["12345678", "87654321"])

# WoS 収録インデックスを付与(任意)
index_sets = scie.discover_index_sets(scie_dir="index")
scie.annotate_papers_indexes(papers, index_sets)

# 集計
year_range = default_eval_year_range()  # 前年を含む直近 5 年
report = summarize_papers(papers, year_range=year_range)
print(report)
```

KAKEN 連携の細かい API は `scopus_tools/kaken.py` を直接参照してください。

---

## プロジェクト構成

```text
scopus_tools/
    __init__.py
    api.py          # ScopusClient(唯一の Scopus ネットワーク境界)
    cli.py          # argparse によるサブコマンドディスパッチ
    core.py         # 純関数: H/G-index, summarize, year range
    kaken.py        # KAKEN (科研費) API クライアント
    linking.py      # Scopus 著者 ↔ KAKEN 研究者の名前マッチング
    mcp_server.py   # MCP サーバ(stdio)
    projects.py     # プロジェクト永続化(JSON ファイル)
    asof.py         # 鮮度判定と取得日の一貫性チェック
    cachedb.py      # SQLite レスポンスキャッシュ(永続化層)
    httpcache.py    # HTTP の継ぎ目(タイムアウト/スロットル/429/キャッシュ)
    mcp_setup.py    # MCP クライアントへの登録(絶対パス解決・鍵の受け渡し)
    scie.py         # Web of Science 収録インデックス判定
    utils.py        # CSV I/O・ロギング・進捗表示
tests/              # pytest スイート
pyproject.toml
```

---

## トラブルシューティング

### `SCOPUS_API_KEY is not set` で即終了

`.env` がリポジトリルートにあること、`SCOPUS_API_KEY=xxx` の書式(クォート不要)に
なっていることを確認してください。読み込みチェック:

```bash
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(bool(os.getenv('SCOPUS_API_KEY')))"
```

### Scopus 認証は通るが結果ゼロ

機関ネットワーク外からのアクセスかもしれません。`api.ScopusClient` は機関契約に紐付くため、
契約外 IP では 0 件が返ります。VPN 経由か Institutional Token の設定が必要です。

### MCP クライアントがサーバに接続できない

よくある原因は順に:

1. **`~/Documents` 等に置いた実行ファイルを登録している。** ログに
   `PermissionError ... pyvenv.cfg` が出ていればこれです。TCC 保護外
   (`uv tool install` の `~/.local`)に入れ直してください
2. **コマンドが絶対パスでない。** MCP クライアントはシェルを経由しないので、
   venv の有効化も `PATH` も引き継がれません。`which scopus-tools` の結果をそのまま使う
3. **MCP SDK が入っていない。** `pip install -e ".[mcp]"` または
   `uv tool install "scopus_tools[mcp] @ ."`(SDK は Python 3.10 以上、本体は 3.12 以上)
4. 独自にツールを追加した場合、stdout に `print()` していないか。
   stdout は JSON-RPC 専用で、1 行でも混ざるとハンドシェイクが壊れます

登録せずに手で確かめるなら、次を実行して数秒固まれば(= 入力待ち)正常です:

```bash
/path/to/scopus-tools mcp
```

### MCP は繋がるがツールが「キーが無い」と返す

シェルの `export` は MCP クライアントに引き継がれません。
`claude mcp add` の `-e SCOPUS_API_KEY=...` か、Claude Desktop の `env` で渡してください。
`.env` はカレントディレクトリ基準でも探しますが、MCP クライアントが設定する
カレントディレクトリは環境依存なので、明示的に渡すのが確実です。

### プロジェクトファイルが壊れて開けない

壊れた JSON はスキップしてログに警告を出します。該当ファイルを手で修復するか、
バックアップから戻してください。各保存はアトミック書き込み(`tempfile + os.replace`)なので
クラッシュ起因の破損は発生しにくい設計です。

### KAKEN 認証は通るが研究者が見つからない

CiNii NRID 検索は日本語氏名でのマッチング精度が高いので、英名(`Hiroyuki Okamura`)で
当たらなければ日本語名(`岡村 寛之`)で再検索してみてください。
