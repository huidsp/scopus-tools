"""scopus_tools/llm.py のプロバイダ層テスト。

OpenAI と Anthropic の SDK 呼び出しはモック。実 API は叩かない。
"""
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# provider_for / required_key_for
# ---------------------------------------------------------------------------

def test_provider_for_gpt():
    from scopus_tools import llm
    assert llm.provider_for("gpt-5.4") == "openai"
    assert llm.provider_for("gpt-4o") == "openai"


def test_provider_for_claude():
    from scopus_tools import llm
    assert llm.provider_for("claude-opus-4-7") == "anthropic"
    assert llm.provider_for("claude-sonnet-4-6") == "anthropic"


def test_provider_for_unknown_raises():
    from scopus_tools import llm
    with pytest.raises(ValueError):
        llm.provider_for("llama-3")
    with pytest.raises(ValueError):
        llm.provider_for("")


def test_required_key_for():
    from scopus_tools import llm
    assert llm.required_key_for("gpt-5.4") == "OPENAI_API_KEY"
    assert llm.required_key_for("claude-opus-4-7") == "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------

def test_complete_openai_uses_openai_sdk():
    from scopus_tools import llm

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="hi from gpt"))]
    )
    with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}), \
         patch("openai.OpenAI", return_value=mock_client):
        result = llm.complete("gpt-5.4", "ping")

    assert result == "hi from gpt"
    args = mock_client.chat.completions.create.call_args.kwargs
    assert args["model"] == "gpt-5.4"
    assert args["messages"][0]["content"] == "ping"
    # json_mode=False なので response_format は付かない
    assert "response_format" not in args


def test_complete_openai_json_mode_sets_response_format():
    from scopus_tools import llm

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"k":1}'))]
    )
    with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}), \
         patch("openai.OpenAI", return_value=mock_client):
        llm.complete("gpt-5.4", "ping", json_mode=True)

    args = mock_client.chat.completions.create.call_args.kwargs
    assert args["response_format"] == {"type": "json_object"}


def test_complete_anthropic_uses_anthropic_sdk():
    from scopus_tools import llm

    fake_block = MagicMock(text="hi from claude")
    mock_resp = MagicMock(content=[fake_block])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy"}, clear=True), \
         patch("anthropic.Anthropic", return_value=mock_client):
        result = llm.complete("claude-opus-4-7", "ping")

    assert result == "hi from claude"
    args = mock_client.messages.create.call_args.kwargs
    assert args["model"] == "claude-opus-4-7"
    assert "max_tokens" in args


def test_complete_anthropic_json_mode_appends_instruction():
    from scopus_tools import llm

    fake_block = MagicMock(text='{"k":1}')
    mock_resp = MagicMock(content=[fake_block])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy"}, clear=True), \
         patch("anthropic.Anthropic", return_value=mock_client):
        llm.complete("claude-opus-4-7", "ping", json_mode=True)

    sent_prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "ping" in sent_prompt
    assert "JSON" in sent_prompt  # 強制指示


def test_complete_raises_when_key_missing():
    from scopus_tools import llm

    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OPENAI_API_KEY", None)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            llm.complete("gpt-5.4", "ping")


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------

def test_stream_openai_yields_accumulated():
    from scopus_tools import llm

    fake_chunks = [
        MagicMock(choices=[MagicMock(delta=MagicMock(content="a"))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="b"))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="c"))]),
    ]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter(fake_chunks)

    with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy"}), \
         patch("openai.OpenAI", return_value=mock_client):
        out = list(llm.stream("gpt-5.4", "ping"))

    assert out == ["a", "ab", "abc"]
    assert mock_client.chat.completions.create.call_args.kwargs.get("stream") is True


def test_stream_anthropic_yields_accumulated():
    from scopus_tools import llm

    # text_stream はデルタ文字列を yield する
    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value.text_stream = iter(["a", "b", "c"])
    mock_stream_ctx.__exit__.return_value = False

    mock_client = MagicMock()
    mock_client.messages.stream.return_value = mock_stream_ctx

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy"}, clear=True), \
         patch("anthropic.Anthropic", return_value=mock_client):
        out = list(llm.stream("claude-opus-4-7", "ping"))

    assert out == ["a", "ab", "abc"]


# ---------------------------------------------------------------------------
# parse_json_response
# ---------------------------------------------------------------------------

def test_parse_json_plain():
    from scopus_tools.llm import parse_json_response
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_json_with_code_fence():
    from scopus_tools.llm import parse_json_response
    text = '```json\n{"a": 2}\n```'
    assert parse_json_response(text) == {"a": 2}


def test_parse_json_with_preamble():
    from scopus_tools.llm import parse_json_response
    text = "Here you go:\n{\"a\": 3}\nThanks!"
    assert parse_json_response(text) == {"a": 3}


def test_parse_json_fallback_empty():
    from scopus_tools.llm import parse_json_response
    assert parse_json_response("no json here") == {}
    assert parse_json_response("") == {}
    assert parse_json_response(None) == {}
