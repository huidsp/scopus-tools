# scopus_tools

Scopus API・科研費 (KAKEN) データベース・LLM (OpenAI / Anthropic) を組み合わせて、
研究者の業績を取得・集計・要約・AI 評価できる Python ツールです。CLI と Gradio WebUI の
両方を備え、人事選考や部局単位の研究者比較ワークフローを想定しています。

---

## 目次

- [機能](#機能)
- [クイックスタート](#クイックスタート)
- [必要要件](#必要要件)
- [インストール](#インストール)
- [Docker での利用](#docker-での利用)
- [API キーの取得と設定](#api-キーの取得と設定)
  - [Scopus API キー (`SCOPUS_API_KEY`)](#scopus-api-キー-scopus_api_key)
  - [OpenAI API キー (`OPENAI_API_KEY`)](#openai-api-キー-openai_api_key)
  - [Anthropic API キー (`ANTHROPIC_API_KEY`)](#anthropic-api-キー-anthropic_api_key)
  - [CiNii アプリケーション ID (`KAKEN_APP_ID`)](#cinii-アプリケーション-id-kaken_app_id)
  - [`.env` ファイルの作成](#env-ファイルの作成)
  - [どのキーがどのコマンドで必要か](#どのキーがどのコマンドで必要か)
- [AI モデル選択](#ai-モデル選択)
- [年範囲の指定](#年範囲の指定)
- [CLI リファレンス](#cli-リファレンス)
- [WebUI](#webui)
  - [プロジェクトディレクトリの管理](#プロジェクトディレクトリの管理)
  - [比較タブ(人事選考向け)](#比較タブ人事選考向け)
- [Python API](#python-api)
- [プロジェクト構成](#プロジェクト構成)
- [トラブルシューティング](#トラブルシューティング)

---

## 機能

- 著者名から Scopus ID を検索(同名異人・複数 ID 分裂への対応込み)
- 複数 Scopus ID をまとめた論文検索・重複除去
- H-index / G-index の計算、年範囲別の論文数・被引用数・筆頭著者数の集計
- 人が読みやすい要約レポート(テキスト / JSON)
- CSV 入力による一括検索・一括集計
- OpenAI / Anthropic Claude による研究分野推定と総合業績評価(ストリーミング出力)
- 科研費 (KAKEN) の研究者検索・獲得課題サマリーと AI 評価への統合
- Gradio WebUI:
  - 階層構造(**プロジェクト → 複数研究者**)で結果を永続化
  - Scopus / KAKEN / AI 評価 / 比較 の 4 タブ
  - 比較タブで複数研究者を表 + AI で横並び評価(人事選考向け)
  - 各結果はクリップボードコピー / Markdown エクスポート対応

---

## クイックスタート

```bash
# 1. クローンと仮想環境
git clone https://github.com/huidsp/scopus-tools.git
cd scopus-tools
python3 -m venv .venv
source .venv/bin/activate

# 2. 依存関係(WebUI 込み)
pip install -e ".[ui,dev]"

# 3. API キーを .env に書く(下のセクション参照)
cp .env.example .env  # サンプルがあれば。無ければ手で作成

# 4. WebUI 起動
scopus-tools webui
# → ブラウザで http://127.0.0.1:7860 が自動で開く
```

---

## 必要要件

- Python 3.9 以上
- API キー(下記参照、最低限 `SCOPUS_API_KEY` と AI 評価用に OpenAI or Anthropic のいずれか)

---

## インストール

### 開発インストール(推奨)

```bash
git clone https://github.com/huidsp/scopus-tools.git
cd scopus-tools
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[ui,dev]"
```

オプション extras:
- `[ui]` — Gradio WebUI 用(`scopus-tools webui` を使うなら必須)
- `[dev]` — `pytest`(テスト実行用)

### 直接インストール(WebUI 不要なら)

```bash
pip install "git+https://github.com/huidsp/scopus-tools.git"
```

---

## Docker での利用

ローカルに Python 環境を作らず、Docker だけで CLI / WebUI を実行できます。
リポジトリ直下に `Dockerfile` と `.dockerignore` が同梱され、GitHub Container Registry (GHCR)
で公式イメージも配布しています(`linux/amd64` + `linux/arm64` マルチアーキ)。

### GHCR から pull(通常はこちら)

`git clone` も `docker build` も不要、`docker pull` だけで使えます:

```bash
# 最新リリース
docker pull ghcr.io/huidsp/scopus-tools:latest

# main 追随版(最新コミット)
docker pull ghcr.io/huidsp/scopus-tools:main

# バージョン固定
docker pull ghcr.io/huidsp/scopus-tools:0.1.0
docker pull ghcr.io/huidsp/scopus-tools:0.1     # マイナーまで固定
```

Apple Silicon Mac でも Linux サーバ (amd64) でも、`docker` 側が自動で正しい arch を選びます。

以降の `docker run` の例では `scopus-tools` のかわりに `ghcr.io/huidsp/scopus-tools:latest`
を指定すれば同様に動きます。

### ローカルビルド(任意)

ソースを改造して試したい場合や、オフライン環境で完結させたい場合:

```bash
git clone https://github.com/huidsp/scopus-tools.git
cd scopus-tools
docker build -t scopus-tools .
```

ベースイメージは `python:3.11-slim`。`gradio` を含む `[ui]` extras までインストール済みなので、
そのまま WebUI も CLI も動きます。

### WebUI を起動

```bash
docker run --rm -p 7860:7860 \
  --env-file .env \
  -v "$HOME/.scopus-tools/projects:/data/projects" \
  ghcr.io/huidsp/scopus-tools:latest
```

- ブラウザで <http://localhost:7860> を開く(コンテナ内では `--inbrowser` は無効化されるので
  自動オープンはしません)
- `--env-file .env` で API キーをランタイム注入(イメージには焼き込まれません)
- `-v` でプロジェクト JSON をホストに永続化(コンテナを消してもデータは残る)

LAN 内の別 PC から触りたいときは `-p 0.0.0.0:7860:7860` でホスト側にも公開してください。

### CLI を実行

`ENTRYPOINT` が `scopus-tools` になっているので、`docker run` の引数がそのまま CLI 引数になります
(以下、ローカルビルドした `scopus-tools` イメージのかわりに `ghcr.io/huidsp/scopus-tools:latest`
を指定しても同じです):

```bash
IMG=ghcr.io/huidsp/scopus-tools:latest

# 著者名から Scopus ID を検索
docker run --rm --env-file .env "$IMG" search --name "Hiroyuki Okamura"

# 業績サマリ
docker run --rm --env-file .env "$IMG" summary 12345678 --years 2021-2025

# AI 評価
docker run --rm --env-file .env "$IMG" eval 12345678 --kaken-auto

# CSV 入出力(ホスト側の作業ディレクトリをマウント)
docker run --rm --env-file .env \
  -v "$PWD:/work" -w /work \
  "$IMG" batch --input authors.csv --output summary.csv
```

### docker compose で常駐させる

毎回長い `docker run` を打ちたくない場合は、リポジトリ直下に以下の `docker-compose.yml` を置くと便利です:

```yaml
services:
  scopus-tools:
    image: ghcr.io/huidsp/scopus-tools:latest
    # ローカルビルドしたい場合は image を消して下記を有効化:
    # build: .
    container_name: scopus-tools
    ports:
      - "7860:7860"
    env_file:
      - .env
    volumes:
      - ~/.scopus-tools/projects:/data/projects
    restart: unless-stopped
```

```bash
docker compose up -d            # WebUI をバックグラウンド起動
docker compose logs -f          # ログ追従
docker compose down             # 停止
```

### よく使うオプション早見表

| 目的 | フラグ |
|---|---|
| API キーをコンテナに渡す | `--env-file .env` または `-e SCOPUS_API_KEY=...` |
| プロジェクト JSON を永続化 | `-v "$HOME/.scopus-tools/projects:/data/projects"` |
| CSV を読み書きしたい | `-v "$PWD:/work" -w /work` |
| 別ポートで WebUI | `-p 8080:7860` |
| LAN 公開 | `-p 0.0.0.0:7860:7860` |

### 注意事項

- **Scopus は機関 IP 認証**。Docker ホスト自体が機関ネットワーク(または機関 VPN 接続済み)で
  ないと、コンテナからも認証が通りません。
- `.env` は **イメージに焼き込まないでください**(`.dockerignore` で除外済)。漏れると鍵が
  一緒に配布されます。配布用イメージを作るときも必ず `--env-file` で外から渡す運用に。
- コンテナ内ユーザは UID 1000 (`appuser`)。ホストのプロジェクトディレクトリの所有者が違う場合は
  `chown -R 1000:1000 ~/.scopus-tools/projects` するか、`docker run --user "$(id -u):$(id -g)"`
  で揃えてください。

### メンテナ向け: イメージ公開フロー

`.github/workflows/docker-publish.yml` が以下のタイミングで自動ビルド & GHCR push します:

| トリガー | publish されるタグ |
|---|---|
| `main` への push | `:main`, `:sha-<short>` |
| `v*` タグの push (例: `v0.1.0`) | `:0.1.0`, `:0.1`, `:latest` |
| Pull Request | (ビルドのみ、push なし) |

リリースの出し方:

```bash
git tag v0.1.0
git push origin v0.1.0
# → GitHub Actions が走り、:0.1.0 / :0.1 / :latest が GHCR に publish される
```

**初回 push 直後にだけ必要な手作業**(2 回目以降は不要):

1. https://github.com/users/huidsp/packages/container/scopus-tools/settings を開く
2. **Danger Zone → Change package visibility → Public** に切替
3. (任意)同ページ **Manage Actions access** に `huidsp/scopus-tools` リポジトリを追加し、
   このリポジトリの Actions からのみ更新できる状態にする

---

## API キーの取得と設定

### Scopus API キー (`SCOPUS_API_KEY`)

論文・著者検索に必須。**ほぼ全コマンドで必要**(`kaken-search`, `kaken-summary`, `webui` を除く)。

1. [Elsevier Developer Portal](https://dev.elsevier.com/) にアクセスしてアカウント登録
2. **My API Key** から新規キーを発行
3. 利用機関の IP からのアクセスが必要(通常は大学等の機関ネットワーク経由)
4. 取得した英数字キーを `SCOPUS_API_KEY` として `.env` に保存

**注意**: Scopus API は機関契約に紐付くため、契約外のネットワークからは認証失敗します。
学外利用には機関 VPN や Institutional Token などの設定が別途必要な場合があります。

### OpenAI API キー (`OPENAI_API_KEY`)

`gpt-*` モデルを使う場合のみ必要(`analyze` / `eval` / WebUI の AI 評価)。

1. [OpenAI Platform](https://platform.openai.com/) でアカウント作成
2. 課金情報を登録(従量課金、最小チャージ $5〜)
3. **API Keys** ページで **Create new secret key**
4. `sk-...` で始まる文字列を `OPENAI_API_KEY` として `.env` に保存

利用料の目安(2026 年現在、`gpt-5.4`): 1 研究者の eval = $0.05〜0.20 程度。

### Anthropic API キー (`ANTHROPIC_API_KEY`)

`claude-*` モデルを使う場合に必要(**既定モデルが `claude-opus-4-7`** なので通常はこちらを推奨)。

1. [Anthropic Console](https://console.anthropic.com/) でアカウント作成
2. 課金情報を登録(従量課金)
3. **API Keys** ページで新規キーを発行
4. `sk-ant-...` で始まる文字列を `ANTHROPIC_API_KEY` として `.env` に保存

利用料の目安: `claude-opus-4-7` は高品質・高価格(評価 1 件 $0.10〜0.50)。
試行錯誤や大量処理は `claude-sonnet-4-6`(中価格)/ `claude-haiku-4-5`(低価格)が向きます。

**OpenAI / Anthropic のいずれか一方** があれば AI 機能は動きます(両方は不要)。

### CiNii アプリケーション ID (`KAKEN_APP_ID`)

科研費(KAKEN)関連機能を使う場合のみ必要(`kaken-search` / `kaken-summary` / `eval --kaken-id` / WebUI KAKEN タブ)。

1. [CiNii API 開発者向けページ](https://support.nii.ac.jp/ja/cinii/api/developer) に従って利用申請
2. 申請承認後、CiNii から発行される appid を `KAKEN_APP_ID` として `.env` に保存

KAKEN API は無料ですが、レートリミットがあるので大量取得時は注意。

### `.env` ファイルの作成

プロジェクトのルート(リポジトリ直下)に `.env` を作成します(`python-dotenv` が起動時に読み込みます):

```env
# 必須: Scopus API キー
SCOPUS_API_KEY=your_scopus_api_key

# AI 評価用(どちらか一方でも可、両方あれば WebUI から選択可)
ANTHROPIC_API_KEY=sk-ant-your_anthropic_key   # 既定モデル(claude-opus-4-7)を使うならこちら
OPENAI_API_KEY=sk-your_openai_key             # gpt-* モデルを使う場合

# 科研費連携用(任意)
KAKEN_APP_ID=your_cinii_application_id
```

**セキュリティ注意**:
- `.env` は **絶対に Git にコミットしないこと**(`.gitignore` に登録済み)
- 共有マシンや CI では環境変数として直接設定するか、Vault / Secrets Manager を使う
- キーが漏洩したら即座にプロバイダ側で無効化・再発行

CLI 起動時に `.env` が無くてもシェルの環境変数(`export SCOPUS_API_KEY=...`)があれば動作します。

### どのキーがどのコマンドで必要か

| コマンド | SCOPUS | OpenAI/Anthropic | KAKEN |
|---|:-:|:-:|:-:|
| `search`, `stats`, `summary`, `batch` | 必須 | — | — |
| `analyze`, `eval` | 必須 | **いずれか必須** (選択モデル次第) | `eval --kaken-id` 時のみ |
| `kaken-search`, `kaken-summary` | — | — | 必須 |
| `webui` | (起動時は不要) | (各 AI 機能を使う時に必要) | (KAKEN タブを使う時に必要) |

CLI は起動時にモデルに応じた必須キーを自動チェックし、未設定なら即座にエラーで終了します。
WebUI は起動でき、画面上部のバナーで欠落キーを警告 + 対応機能だけスキップされます。

---

## AI モデル選択

CLI(`analyze` / `eval`)と WebUI(3. AI 評価 / 4. 比較 タブ)で次のモデルから選べます。
**既定は `claude-opus-4-7`** で、品質重視の人事選考用途を想定しています。

| モデル名 | プロバイダ | 用途 | 速度 | 価格 |
|---|---|---|---|---|
| `claude-opus-4-7` (既定) | Anthropic | 高品質、人事選考など重要評価 | 中 | 高 |
| `claude-sonnet-4-6` | Anthropic | バランス、複数評価の取り回し | 速 | 中 |
| `claude-haiku-4-5` | Anthropic | 高速・低コスト、試行錯誤 | 最速 | 低 |
| `gpt-5.4` | OpenAI | 第 2 案として読み比べる | 中 | 中 |

CLI:

```bash
scopus-tools eval 12345678                              # 既定 (claude-opus-4-7)
scopus-tools eval 12345678 --model claude-sonnet-4-6    # コスト抑制
scopus-tools eval 12345678 --model gpt-5.4              # OpenAI でセカンドオピニオン
scopus-tools analyze 12345678 --model claude-haiku-4-5
```

WebUI: 3. AI 評価タブ・4. 比較タブの「AI モデル」ドロップダウンで選択(プロジェクトに保存・復元)。

---

## 年範囲の指定

`--years`(`stats` の `--year`)は次のいずれの書式でも受理します:

```text
2021-2025
2021,2025
2021:2025
[2021,2025]
```

`summary` / `batch` / `eval` で `--years` を省略した場合は **前年を含む直近 5 年** が使われます
(例: 2026 年実行 → 2021–2025)。進行中の今年を含めるとデータが半年ぶんしか揃わないため、
既定では除外しています。

---

## CLI リファレンス

エントリーポイントは `scopus-tools` です(`pip install` 後)。または `python -m scopus_tools.cli` でも同じ。

### `search` — 著者名から Scopus ID を検索

```bash
# 単体検索
scopus-tools search --name "Hiroyuki Okamura"

# CSV 一括検索(Name 列必須)
scopus-tools search --input authors.csv --output author_ids.csv
```

出力 CSV: `Name`, `Scopus ID`, `Affiliation` の 3 列。

### `stats` — 年範囲ごとの集計

```bash
scopus-tools stats --years 2020-2024 --input author_ids.csv --output stats.csv
```

入力 CSV: `Name`, `Scopus ID`, (任意で `Affiliation`)。出力に論文タイプ別件数(Article / Review 等)も含む。

### `summary` — 人間可読の業績サマリ

```bash
scopus-tools summary 12345678,87654321
scopus-tools summary 12345678 --years 2021-2025
scopus-tools summary --input author_ids.csv --output reports.txt  # CSV 一括処理
scopus-tools summary 12345678 --format json --output summary.json  # JSON 出力
```

研究歴 / 引用指標 / 評価期間集計 / 被引用上位 5 件を出力。

### `batch` — CSV 入出力の一括サマリ

```bash
scopus-tools batch --input author_ids.csv --output summary.csv
scopus-tools batch --input author_ids.csv --output summary.csv --years 2021-2025
```

出力 CSV: Name / Scopus IDs / Affiliation / Research Years / Start Year / Total Papers / Total Citations /
Total First Author / Recent 5Y Papers / Recent 5Y Citations / Recent 5Y First Author / H-index / G-index。

### `analyze` — AI による研究専門性推定

```bash
scopus-tools analyze 12345678,87654321
scopus-tools analyze 12345678 --lang en
scopus-tools analyze 12345678 --model claude-sonnet-4-6
```

論文タイトル群から研究分野と主要技術用語を AI に推定させる(短いプロンプト)。

### `eval` — AI 総合業績評価

```bash
# 既定: 推定分野での相対評価、KAKEN_APP_ID 設定済みなら名前自動マッチング
scopus-tools eval 12345678 --years 2020-2024

# KAKEN 研究者番号を明示
scopus-tools eval 12345678 --kaken-id 80401243

# 非対話で先頭候補 / KAKEN 連携 OFF
scopus-tools eval 12345678 --kaken-auto
scopus-tools eval 12345678 --no-kaken

# 複数研究者を JSON で一括
scopus-tools eval --input author_ids.csv --format json --output evals.json

# モデル指定
scopus-tools eval 12345678 --model gpt-5.4
```

`evaluate_achievements_stream` で AI に渡す前に **分野推定** を 1 回呼んで分野バイアス補正の
材料にします。`--kaken-id` 指定 / 自動マッチング成功時は科研費獲得実績(代表/分担、種目、総額)も
プロンプトに含めます。

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

役割別・種目別の件数、配分額合計、課題一覧を表示。`--role principal_investigator` で代表者のみに絞れます。

### `webui` — Gradio WebUI 起動

```bash
scopus-tools webui                                 # http://127.0.0.1:7860 を自動で開く
scopus-tools webui --port 8080                     # ポート変更
scopus-tools webui --host 0.0.0.0                  # LAN 公開
scopus-tools webui --share                         # Gradio 公開共有 URL を発行
scopus-tools webui --projects-dir ./demo_projects  # プロジェクト保存先を切替
```

詳細は [WebUI セクション](#webui) を参照。

---

## WebUI

Gradio による Web 画面で、CLI の機能を統合して **複数研究者の業績を蓄積・比較** できます。
人事選考や部局レビューのような **複数候補を見比べる** ワークフロー向けの構成です。

### 画面構成

- **左サイドバー**: 階層 2 段
  - 📁 **プロジェクト**(Dropdown): 部局・委員会・ラボなどの "束ね" 単位
  - 👤 **研究者**(Radio): 選択中プロジェクトに登録された人たち
  - 各段に新規作成・リネーム・削除(削除は 2 段階確認)
- **右メイン**: 4 タブ
  - **1. Scopus**: 著者名検索 → 候補チェック → 業績集計
  - **2. KAKEN**: 研究者名検索 or 8 桁番号直接入力 → 課題取得
  - **3. AI 評価**: 集計結果 + KAKEN を踏まえた AI 総合評価(ストリーミング)
  - **4. 比較**: プロジェクト内研究者を複数選択 → 比較表 + AI 比較評価

### プロジェクトディレクトリの管理

1 プロジェクト = 1 つの JSON ファイル。1 プロジェクトに複数の研究者を登録でき、
各研究者の Scopus 集計 / KAKEN 課題 / AI 評価 / 比較結果が同じファイルに永続化されます。

#### 保存場所

- **既定**: `~/.scopus-tools/projects/<project-name>.json`
- **任意**: `scopus-tools webui --projects-dir <PATH>` で別ディレクトリに

例:

```bash
# 部局ごとにフォルダを分けたい場合
scopus-tools webui --projects-dir ~/projects/engineering_dept/
scopus-tools webui --projects-dir ~/projects/medical_dept/

# プロジェクトを Git で管理したい場合(自分専用リポジトリで)
cd ~/my-evaluations
git init
scopus-tools webui --projects-dir ./projects/
```

#### ファイル形式

人間可読 JSON。テキストエディタで開いて手動編集も可能(壊した場合は壊れたファイルだけスキップ):

```json
{
  "name": "広島大 CS 2026",
  "created_at": "2026-05-26T10:00:00",
  "updated_at": "2026-05-26T11:30:00",
  "researchers": [
    {
      "name": "Hiroyuki Okamura",
      "scopus": { "selected_ids": [...], "papers": [...], "report": {...}, "year_range": [2021, 2025] },
      "kaken": { "kaken_ids": [...], "grants": [...] },
      "ai": { "model": "claude-opus-4-7", "evaluation": "...", "field_ctx": {...} }
    }
  ],
  "comparison": {
    "selected_names": ["Hiroyuki Okamura", "Taro Tanaka"],
    "model": "claude-opus-4-7",
    "table_md": "...",
    "ai_evaluation": "..."
  }
}
```

#### バックアップ・共有

- 単純なファイルコピーで OK(`cp ~/.scopus-tools/projects/foo.json ~/backup/`)
- 機微情報(個人氏名・所属など)を含むので扱いに注意
- 別 PC で開きたい場合は同じディレクトリにコピーすればそのまま読める

#### 自動保存タイミング

- 各タブの「集計を実行」「AI 評価を実行」完了時に自動保存
- プロジェクト・研究者の新規作成 / リネーム / 削除も即時保存
- ブラウザを閉じても次回起動時に全データが復元

### 操作の基本フロー

1. サイドバーで「新規プロジェクト」名(例: `広島大 CS 2026`)を入力 → ➕
2. 続けて「新規研究者」名(例: `Hiroyuki Okamura`)を入力 → ➕
3. 右側 **1. Scopus** タブで検索 → 候補チェック(同一人物が複数 ID に分かれている場合は全てチェック)→
   「Scopus 集計を実行」
4. **2. KAKEN** タブで同様に検索・取得(KAKEN_APP_ID 未設定なら丸ごとスキップ)
5. **3. AI 評価** タブで「AI 評価を実行」(数十秒、ストリーミング表示)
6. 別の研究者を「新規研究者」で追加して 3〜5 を繰り返す
7. **4. 比較** タブで複数選択 → 「比較表を生成」「AI 比較評価を実行」

### 比較タブ(人事選考向け)

プロジェクト内の研究者を **最大 10 名** までチェックして横並び比較できます。

- **比較表**: H-index / G-index / 論文数 / 被引用数 / KAKEN 件数・代表者・総額 などを表で表示。
  Markdown でコピー / エクスポート → Excel にそのまま貼り付け可
- **AI 比較評価**: 各人の **推定研究分野** を踏まえて分野バイアスを補正した相対評価を
  AI に依頼。強み・懸念・総合ランキングを Markdown で生成(ストリーミング)
- **評価の観点・追加指示** テキストエリアで自由な指示を追加可能:
  「テニュアトラック前提で 5 年先のポテンシャルも見て」「ランキングは不要、強みと懸念だけ」など
- 比較対象・表・AI 評価はプロジェクトの `comparison` セクションに保存され再起動後も復元

### 結果のコピー / エクスポート

各タブの結果の下に **📋 コピー** と **📤 エクスポート** ボタン:
- コピー: ブラウザのクリップボードに Markdown 形式で貼り付け
- エクスポート: タイムスタンプ付き `.md` ファイルをブラウザ経由でダウンロード

---

## Python API

```python
from scopus_tools.api import ScopusClient
from scopus_tools.core import summarize_papers, default_eval_year_range
from scopus_tools.ai_engine import estimate_expertise, evaluate_achievements

client = ScopusClient()  # SCOPUS_API_KEY を env から読む

given, surname = client.get_author_profile("12345678")
papers = client.search_papers(["12345678", "87654321"])

# 集計
year_range = default_eval_year_range()  # 前年を含む直近 5 年
report = summarize_papers(papers, year_range=year_range)
print(report)

# AI 評価(model 省略時は claude-opus-4-7)
analysis = estimate_expertise(papers, lang="ja")
print(analysis)

evaluation = evaluate_achievements(papers, report, lang="ja",
                                   model="claude-sonnet-4-6",
                                   extra_instructions="研究の独自性を重視")
print(evaluation)
```

KAKEN 連携 / プロバイダ抽象化のさらに細かい API は `scopus_tools/kaken.py`, `scopus_tools/llm.py` を
直接参照してください。

---

## プロジェクト構成

```text
scopus_tools/
    __init__.py
    ai_engine.py    # OpenAI / Anthropic を呼ぶ高レベル評価関数
    api.py          # ScopusClient(唯一の Scopus ネットワーク境界)
    cli.py          # argparse によるサブコマンドディスパッチ
    core.py         # 純関数: H/G-index, summarize, year range
    kaken.py        # KAKEN (科研費) API クライアント
    linking.py      # Scopus 著者 ↔ KAKEN 研究者名前マッチング
    llm.py          # OpenAI / Anthropic 共通レイヤ(complete/stream)
    projects.py     # WebUI 用プロジェクト永続化(JSON ファイル)
    utils.py        # CSV I/O・ロギング・進捗表示
    webui.py        # Gradio Web UI(4 タブ構成)
tests/              # pytest スイート
pyproject.toml
```

---

## トラブルシューティング

### `SCOPUS_API_KEY is not set` で即終了

`.env` がリポジトリルートにあること、`SCOPUS_API_KEY=xxx` の書式(クォート不要)になっていることを
確認。CLI からは `python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(bool(os.getenv('SCOPUS_API_KEY')))"` で読み込みチェック可能。

### Scopus 認証は通るが結果ゼロ

機関ネットワーク外からのアクセスかも。`api.ScopusClient` は機関契約に紐付くため、契約外 IP では
0 件が返ります。VPN 経由か Institutional Token の設定が必要です。

### `ANTHROPIC_API_KEY is not set (required for model claude-opus-4-7)`

既定モデルが `claude-opus-4-7` です。Anthropic キーを `.env` に追加するか、
`--model gpt-5.4`(OpenAI を使う)を明示してください。

### WebUI が起動しない / `gradio` ImportError

`pip install -e ".[ui]"` で UI extras を入れ直してください。Gradio 4.44.x は `huggingface_hub < 1.0`
に依存するため、別ツールで `huggingface_hub` を上げた場合は `pip install "huggingface_hub<1.0"` で
ピン留めし直し。

### プロジェクトファイルが壊れて開けない

WebUI は壊れた JSON はスキップしてログに警告を出します。該当ファイルを手で開いて修復するか、
バックアップから戻してください。各保存はアトミック書き込み(`tempfile + os.replace`)なので
クラッシュ起因の破損は発生しにくい設計です。

### KAKEN 認証は通るが研究者が見つからない

CiNii NRID 検索は日本語氏名でのマッチング精度が高いので、英名(`Hiroyuki Okamura`)で当たらなければ
日本語名(`岡村 寛之`)で再検索してみてください。WebUI の 2. KAKEN タブは英日両方の自由検索に
対応しています。
