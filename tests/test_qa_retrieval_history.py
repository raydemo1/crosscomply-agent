import json

from law_agent.review.tool_calling import CONTEXT_PROMPT, ToolCallingAgent, ToolSession
from scripts.eval_qa_retrieval_chain import visible_texts


def test_two_searches_keep_fifteen_unique_originals_in_native_messages():
    hits = [{"chunk_id": str(i), "source_id": "law", "text": f"原文第{i}段"} for i in range(15)]
    hits[2]["article_no"] = "第十三条"
    session = ToolSession()
    first = session.context_delta({"state": {"evidence": hits[:10], "evidence_sources": {"law": {"title": "法律"}}}})
    session.messages.append({"role": "user", "content": json.dumps(first, ensure_ascii=False)})
    original = session.messages[0]["content"]
    session.complete({"chunk_ids": [h["chunk_id"] for h in hits[5:]]},
                     {"state": {"evidence": hits, "evidence_sources": {"law": {"title": "法律"}}}})
    messages = ToolCallingAgent.messages(None, session, prompt="调查提示词")
    assert messages[0].content == "调查提示词" + CONTEXT_PROMPT
    assert messages[1].content == original
    assert len(json.loads(messages[-1].content)["state"]["evidence"]) == 5
    payload = {"messages": [{"role": m.role, "content": m.content} for m in messages]}
    local, sources, web = visible_texts(payload)
    assert len(local) == 15
    assert local["2"]["article_no"] == "第十三条"
    assert local["2"]["text"] == hits[2]["text"]
    assert sources["law"]["title"] == "法律"
    assert not web


def test_web_excerpts_and_updated_readings_remain_distinct_from_navigation_previews():
    messages = [
        {"role": "tool", "content": json.dumps({"state": {"evidence": [{"chunk_id": "one", "text": "原文"}]},
          "observation": {"keyword_candidates": [{"chunk_id": "preview", "preview": "目录预览"}]}})},
        {"role": "tool", "content": json.dumps({"state": {"evidence": [{"chunk_id": "one", "text": "片段", "full_article_text": "完整条文"}]},
          "observation": {"findings": [{"url": "https://example.com/official", "excerpt": "网页原文"}]}})},
    ]
    local, _, web = visible_texts({"messages": messages})
    assert len(local) == 2
    assert local["one"]["text"] == "原文"
    assert any(h.get("full_article_text") == "完整条文" for h in local.values())
    assert set(web) == {"web:https://example.com/official"}


def test_repeated_web_url_preserves_earlier_excerpt_without_counting_identical_repeats():
    excerpts = ["第一轮看到的原文", "第二轮不同原文", "第二轮不同原文"]
    messages = [{"role": "tool", "content": json.dumps({"observation": {"findings": [
        {"url": "https://example.com/law", "excerpt": text}]}})} for text in excerpts]
    _, _, web = visible_texts({"messages": messages})
    assert len(web) == 2
    assert {h["excerpt"] for h in web.values()} == set(excerpts)
