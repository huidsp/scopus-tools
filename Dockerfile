# syntax=docker/dockerfile:1.6

# ---- Base ----------------------------------------------------------------
# Python 3.11 slim:
#   - Avoids the LibreSSL/urllib3 警告 (macOS 環境固有).
#   - gradio / huggingface_hub の新しめのバージョンとも互換.
FROM python:3.11-slim AS base

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

# `[ui]` extra で gradio を入れる. test 依存は本番イメージには入れない.
RUN pip install --upgrade pip \
 && pip install ".[ui]"

# ---- Runtime -------------------------------------------------------------
# 非 root ユーザで実行
RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /data/projects /data/index \
 && chown -R appuser:appuser /data
USER appuser

# WebUI のデフォルトポート
EXPOSE 7860

# プロジェクト JSON の永続化と, WoS インデックス CSV(SCIE/SSCI/AHCI/ESCI)用のボリューム.
# index 側は `./index:/data/index` をマウントして CSV を置く運用.
VOLUME ["/data/projects", "/data/index"]

# tini で PID 1 を引き取り, シグナルを正しく伝搬させる.
ENTRYPOINT ["/usr/bin/tini", "--", "scopus-tools"]

# デフォルトは WebUI 起動. コンテナ外からアクセスできるよう 0.0.0.0 にバインド.
# CLI を使いたいときは `docker run ... search --name "..."` のように上書き可能.
CMD ["webui", "--host", "0.0.0.0", "--port", "7860", \
     "--projects-dir", "/data/projects", \
     "--scie-dir", "/data/index"]
