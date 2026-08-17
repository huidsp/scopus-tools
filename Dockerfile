# syntax=docker/dockerfile:1.6

# ---- Base ----------------------------------------------------------------
# Python 3.12 slim:
#   - パッケージの requires-python (>=3.12) に合わせる.
#   - Avoids the LibreSSL/urllib3 警告 (macOS 環境固有).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ビルドに必要な最小限のパッケージ. pandas は manylinux wheel が来るので gcc 等は不要.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Dependencies (キャッシュ用に別レイヤ) -------------------------------
# pyproject.toml だけ先に入れてもバージョン解決には十分.
COPY pyproject.toml README.md ./
COPY scopus_tools ./scopus_tools

# `[mcp]` extra で MCP SDK を入れる. test 依存は本番イメージには入れない.
RUN pip install --upgrade pip \
 && pip install ".[mcp]"

# ---- Runtime -------------------------------------------------------------
# 非 root ユーザで実行
RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /data/projects /data/index /data/cache \
 && chown -R appuser:appuser /data
USER appuser

# レスポンスキャッシュはコンテナ外に置く(でないと毎回クォータを消費し直す).
ENV SCOPUS_TOOLS_CACHE_DB=/data/cache/cache.sqlite3

# プロジェクト JSON・キャッシュの永続化と, WoS インデックス CSV 用のボリューム.
# index 側は `./index:/data/index` をマウントして CSV を置く運用.
VOLUME ["/data/projects", "/data/index", "/data/cache"]

# tini で PID 1 を引き取り, シグナルを正しく伝搬させる.
ENTRYPOINT ["/usr/bin/tini", "--", "scopus-tools"]

# サブコマンドは実行時に指定する. ネットワークポートは公開しない
# (MCP は stdio なので常駐サービスにはならない).
#
#   MCP サーバとして (MCP クライアントから起動される):
#     docker run -i --rm --env-file .env \
#       -v ./index:/data/index:ro -v ./projects:/data/projects \
#       scopus-tools mcp --scie-dir /data/index --projects-dir /data/projects
#
#   CLI として:
#     docker run --rm --env-file .env scopus-tools search --name "Taro Tanaka"
CMD ["--help"]
