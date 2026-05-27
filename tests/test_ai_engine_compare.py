"""AI 比較評価(compare_researchers_stream)のテスト。OpenAI / 分野推定はモック。"""
import os
from unittest.mock import MagicMock, patch


def _stub_researcher(name, h=10, citations=100, total=20):
    return {
        "name": name,
        "report": {
            "total_count": total, "total_citations": citations,
            "h_index": h, "g_index": h + 2,
            "recent_count": total // 2, "recent_citations": citations // 2,
            "recent_first_author": 3, "research_years": 8, "start_year": 2018,
        },
        "papers": [
            {"title": f"{name}'s paper {i}", "citations": citations - i*10,
             "year": 2018 + i, "journal": "Journal X"}
            for i in range(3)
        ],
        "grants": [],
        "field_ctx": {"field": "計算機科学", "sub_fields": ["NLP"],
                      "citation_norm": "中", "hindex_norm": "中堅10〜20",
                      "pub_rate_norm": "年5本", "notes": ""},
    }


def test_build_compare_prompt_includes_each_candidate_and_fields():
    from scopus_tools.ai_engine import _build_compare_prompt

    items = [_stub_researcher("Alice"), _stub_researcher("Bob")]
    prompt = _build_compare_prompt(items, lang="ja")
    assert "候補 1: Alice" in prompt
    assert "候補 2: Bob" in prompt
    # 分野が含まれる
    assert "計算機科学" in prompt
    # 比較観点のセクション
    assert "比較・評価の観点" in prompt
    assert "総合ランキング" in prompt
    # 言語指定
    assert "ja" in prompt


def _stream_yield(strings):
    """llm.stream 用のフェイクジェネレータ(累積文字列を yield)。"""
    def _gen(model, prompt, **kw):
        acc = ""
        for s in strings:
            acc += s
            yield acc
    return _gen


def test_compare_researchers_stream_yields_accumulated_text():
    from scopus_tools import ai_engine

    items = [_stub_researcher("Alice"), _stub_researcher("Bob")]

    from scopus_tools import llm
    default_key = llm.required_key_for(llm.DEFAULT_MODEL)
    with patch.dict(os.environ, {default_key: "dummy"}, clear=True), \
         patch("scopus_tools.llm.stream", side_effect=_stream_yield(["Hello ", "world", "!"])) as mock_stream:
        outputs = list(ai_engine.compare_researchers_stream(items, lang="ja"))

    # チャンクごとに累積文字列が yield される
    assert outputs == ["Hello ", "Hello world", "Hello world!"]

    # 既定モデルで呼ばれる
    assert mock_stream.call_args.args[0] == llm.DEFAULT_MODEL


def test_compare_researchers_stream_uses_specified_model():
    """model 指定が llm.stream に伝播する。"""
    from scopus_tools import ai_engine

    items = [_stub_researcher("Alice")]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy"}, clear=True), \
         patch("scopus_tools.llm.stream", side_effect=_stream_yield(["ok"])) as mock_stream:
        list(ai_engine.compare_researchers_stream(items, lang="ja",
                                                  model="claude-opus-4-7"))
    assert mock_stream.call_args.args[0] == "claude-opus-4-7"


def test_compare_researchers_stream_calls_field_inference_when_missing():
    """field_ctx 未設定なら _infer_field_context が呼ばれる。"""
    from scopus_tools import ai_engine, llm

    item = _stub_researcher("Alice")
    item["field_ctx"] = None  # 未推定

    default_key = llm.required_key_for(llm.DEFAULT_MODEL)
    with patch.dict(os.environ, {default_key: "dummy"}, clear=True), \
         patch("scopus_tools.llm.stream", side_effect=_stream_yield(["ok"])), \
         patch("scopus_tools.ai_engine._infer_field_context",
               return_value={"field": "テスト分野"}) as infer_mock:
        list(ai_engine.compare_researchers_stream([item], lang="ja"))

    infer_mock.assert_called_once()


def test_compare_researchers_stream_no_api_key():
    from scopus_tools import ai_engine, llm

    default_key = llm.required_key_for(llm.DEFAULT_MODEL)
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop(default_key, None)
        outputs = list(ai_engine.compare_researchers_stream([_stub_researcher("X")]))
    assert len(outputs) == 1
    assert default_key in outputs[0] and "Skipping" in outputs[0]


def test_build_compare_prompt_includes_extra_instructions():
    from scopus_tools.ai_engine import _build_compare_prompt

    items = [_stub_researcher("Alice"), _stub_researcher("Bob")]
    prompt = _build_compare_prompt(items, lang="ja",
                                   extra_instructions="ランキングは不要、強みと懸念だけ")
    assert "評価の観点・追加指示" in prompt
    assert "ランキングは不要、強みと懸念だけ" in prompt


def test_build_eval_prompt_includes_extra_instructions():
    from scopus_tools.ai_engine import _build_eval_prompt

    papers = [{"title": "p1", "citations": 10, "year": 2020, "journal": "J"}]
    report = {
        "total_count": 1, "total_citations": 10, "h_index": 1, "g_index": 1,
        "research_years": 2, "start_year": 2020,
        "recent_count": 1, "recent_citations": 10, "recent_first_author": 1,
    }
    prompt = _build_eval_prompt(papers, report, lang="ja", grants=None,
                                field_ctx={"field": "テスト分野"},
                                extra_instructions="教育能力よりも研究力を重視")
    assert "評価の観点・追加指示" in prompt
    assert "教育能力よりも研究力を重視" in prompt


def test_evaluate_achievements_stream_accepts_field_ctx_and_extra():
    """field_ctx を渡すと _infer_field_context が呼ばれない、extra も伝播。"""
    from scopus_tools import ai_engine

    captured_prompts = []

    def fake_stream(model, prompt, **kw):
        captured_prompts.append(prompt)
        yield "ok"

    papers = [{"title": "p1", "citations": 5, "year": 2020}]
    report = {"total_count": 1, "total_citations": 5, "h_index": 1, "g_index": 1,
              "research_years": 1, "start_year": 2020,
              "recent_count": 1, "recent_citations": 5, "recent_first_author": 0}

    from scopus_tools import llm
    default_key = llm.required_key_for(llm.DEFAULT_MODEL)
    with patch.dict(os.environ, {default_key: "dummy"}, clear=True), \
         patch("scopus_tools.llm.stream", side_effect=fake_stream), \
         patch("scopus_tools.ai_engine._infer_field_context") as infer_mock:
        list(ai_engine.evaluate_achievements_stream(
            papers, report, lang="ja",
            field_ctx={"field": "事前推定"},
            extra_instructions="観点 X を重視",
        ))

    # field_ctx 渡したので _infer_field_context は呼ばれない
    infer_mock.assert_not_called()
    # extra_instructions がプロンプトに含まれていることを確認
    assert captured_prompts and "観点 X を重視" in captured_prompts[0]


def test_set_project_comparison_save_load(tmp_path):
    from scopus_tools.projects import (
        ProjectStore, empty_project, set_project_comparison,
    )

    store = ProjectStore(str(tmp_path))
    p = empty_project("Lab")
    set_project_comparison(p, {
        "selected_names": ["A", "B"],
        "lang": "ja",
        "table_md": "| name |\n|---|\n| A |\n| B |",
        "ai_evaluation": "## Ranking\n1. A\n2. B",
    })
    store.save("Lab", p)

    loaded = store.load("Lab")
    assert loaded["comparison"]["selected_names"] == ["A", "B"]
    assert "Ranking" in loaded["comparison"]["ai_evaluation"]
    assert loaded["comparison"]["updated_at"]
