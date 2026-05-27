"""LLM provider abstraction for scopus-tools.

OpenAI と Anthropic(Claude)を統一インターフェイスでラップする薄い層。
ai_engine.py からは `llm.complete()` / `llm.stream()` だけを呼ぶ。

プロバイダはモデル名のプレフィックスで自動判定:
  - gpt-*   → OpenAI
  - claude-* → Anthropic
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

SUPPORTED_MODELS = [
    "claude-opus-4-7",    # 高品質(既定)
    "claude-sonnet-4-6",  # バランス
    "claude-haiku-4-5",   # 高速・低コスト
    "gpt-5.4",            # OpenAI
]
DEFAULT_MODEL = "claude-opus-4-7"

_DEFAULT_MAX_TOKENS = 8192


def provider_for(model):
    """モデル名から "openai" / "anthropic" を返す。未知なら ValueError。"""
    if not model:
        raise ValueError("model must be a non-empty string")
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    raise ValueError(f"Unknown model: {model}")


def required_key_for(model):
    """選択モデルに必要な環境変数名を返す。"""
    p = provider_for(model)
    return {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[p]


def _ensure_key(model):
    key = required_key_for(model)
    if not os.getenv(key):
        raise RuntimeError(f"{key} is not set (required for model {model})")


_JSON_INSTRUCTION = (
    "\n\nReturn ONLY a single valid JSON object. "
    "Do not include markdown code fences, preamble, or trailing commentary."
)


def complete(model, prompt, *, json_mode=False, max_tokens=_DEFAULT_MAX_TOKENS):
    """非ストリーミング completion。文字列を返す。

    json_mode=True で「JSON のみ返せ」を強制(OpenAI は response_format で、
    Anthropic はプロンプト末尾に注記を付ける)。
    """
    p = provider_for(model)
    _ensure_key(model)

    if p == "openai":
        from openai import OpenAI

        client = OpenAI()
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    # Anthropic
    from anthropic import Anthropic

    client = Anthropic()
    final_prompt = prompt + _JSON_INSTRUCTION if json_mode else prompt
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": final_prompt}],
    )
    # response.content は TextBlock のリスト。最初の text を返す
    parts = getattr(resp, "content", []) or []
    return "".join(getattr(p, "text", "") for p in parts)


def stream(model, prompt, *, max_tokens=_DEFAULT_MAX_TOKENS):
    """ストリーミング generator。チャンクごとに累積文字列を yield する。

    OpenAI / Anthropic の SDK 差を吸収し、呼び出し側は同じ累積パターンで扱える。
    """
    p = provider_for(model)
    _ensure_key(model)

    if p == "openai":
        from openai import OpenAI

        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        collected = ""
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                collected += delta
                yield collected
        if not collected:
            yield ""
        return

    # Anthropic
    from anthropic import Anthropic

    client = Anthropic()
    collected = ""
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as s:
        for delta in s.text_stream:
            if delta:
                collected += delta
                yield collected
    if not collected:
        yield ""


def parse_json_response(text):
    """LLM の JSON 応答を頑健にパース。

    1. そのまま json.loads を試す
    2. 失敗したら最初の '{' から最後の '}' までを抽出して再試行
    3. それでも失敗したら空 dict を返す(現行コードのフォールバック挙動を維持)
    """
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            logger.warning("JSON parse failed even after brace extraction")
    return {}
