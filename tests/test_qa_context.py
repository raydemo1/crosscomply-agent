from copy import deepcopy

from law_agent.review.agent import read_material_page
from law_agent.review.qa_context import (
    conversation_context,
    material_package,
    material_passages,
    merge_material_passages,
)


def state_for(materials, offsets):
    return {"steps": [{"action": "read_material", "observation": read_material_page(material_package(materials), offset)}
                      for offset in offsets]}


def test_read_windows_keep_version_offsets_and_merge_overlapping_originals():
    materials = [{"id": "v1", "filename": "a.txt", "parsed_text": "甲" * 12050},
                 {"id": "v2", "filename": "a.txt", "parsed_text": "乙" * 20}]
    passages = material_passages(state_for(materials, [0, 11000, 12000]), materials)
    assert passages == [{"material_version_id": m["id"], "filename": m["filename"], "offset": 0,
                         "text": m["parsed_text"]} for m in materials]
    snapshot = deepcopy(passages)
    assert merge_material_passages(passages * 2) == passages
    assert snapshot == passages


def test_partial_reads_do_not_expose_unread_material_or_stale_text():
    materials = [{"id": "v1", "filename": "a.txt", "parsed_text": "甲" * 12050},
                 {"id": "v2", "filename": "b.txt", "parsed_text": "未读内容"}]
    state = state_for(materials, [0])
    passages = material_passages(state, materials)
    assert len(passages) == 1 and passages[0]["material_version_id"] == "v1"
    assert len(passages[0]["text"]) < 12050
    assert material_passages(state, [{**materials[0], "parsed_text": "已变化"}, materials[1]]) == []


def test_followup_retains_run_local_citations_and_selected_original_inputs():
    material = {"id": "v1", "filename": "facts.txt", "parsed_text": "用户明确：存在集体合同"}
    previous = {"id": "r1", "status": "completed", "question": "先按2023年规则回答", "material_ids": ["v1"],
                "state": state_for([material], [0]), "answer": "旧回答[依据1]",
                "result": {"citations": [{"ref": "依据1", "source_id": "old-law", "title": "旧版", "url": "https://example.test",
                                          "article_no": "第四条", "excerpt": "不自动当作本轮法源"}]}}
    run = {"id": "r2", "material_ids": ["v1"]}
    future = {**previous, "id": "r3", "question": "未来的问题"}
    conversation = {"materials": [material], "runs": [previous, run, future]}
    snapshot = deepcopy(conversation)
    history, passages = conversation_context(conversation, run)
    assert len(history) == 1
    assert history[0]["turn_id"] == "r1"
    assert history[0]["user_statement"] == previous["question"]
    assert history[0]["previous_citation_locations"]["依据1"]["source_id"] == "old-law"
    assert "excerpt" not in history[0]["previous_citation_locations"]["依据1"]
    assert passages[0]["text"] == material["parsed_text"]
    assert conversation == snapshot
    run["material_ids"] = []
    history, passages = conversation_context(conversation, run)
    assert passages == [] and "previous_model_answer" not in history[0]
    assert "previous_citation_locations" not in history[0]


def test_new_attachment_version_cannot_inherit_the_old_version_text():
    old = {"id": "v1", "filename": "facts.txt", "parsed_text": "旧材料"}
    new = {"id": "v2", "filename": "facts.txt", "parsed_text": "新材料"}
    previous = {"id": "r1", "status": "completed", "question": "之前的问题", "material_ids": ["v1"],
                "state": state_for([old], [0]), "answer": "基于旧材料的结论", "result": {"citations": []}}
    run = {"id": "r2", "material_ids": ["v2"]}
    history, passages = conversation_context({"materials": [old, new], "runs": [previous, run]}, run)
    assert passages == [] and "previous_model_answer" not in history[0]


def test_long_conversation_preserves_early_user_premises_and_later_correction():
    statements = ["已确认依法制定劳动规章制度，集体合同存在。", *[f"追问{i}" for i in range(8)],
                  "更正：集体合同是否存在不清楚，劳动规章制度仍已确认。"]
    previous = [{"id": f"r{i}", "status": "completed", "question": statement, "material_ids": [],
                 "answer": "历史模型分析", "result": {}, "state": {}}
                for i, statement in enumerate(statements)]
    run = {"id": "current", "material_ids": []}
    history, _ = conversation_context({"materials": [], "runs": [*previous, run]}, run)
    assert [entry["user_statement"] for entry in history] == statements


def test_clarification_options_are_model_questions_not_user_facts():
    questions = [{"question": "是否存在集体合同？", "selection": "single", "options": ["存在", "不存在"]}]
    previous = {"id": "r1", "status": "completed", "question": "员工信息出境如何处理？", "material_ids": [],
                "answer": "是否存在集体合同？\n存在 / 不存在", "result": {"questions": questions}, "state": {}}
    run = {"id": "r2", "material_ids": []}
    history, _ = conversation_context({"materials": [], "runs": [previous, run]}, run)
    assert history[0]["previous_model_questions"] == questions
    assert history[0]["user_statement"] == previous["question"]
    assert history[0]["previous_model_answer"] == previous["answer"]


def test_failed_turn_preserves_user_correction_without_partial_model_output():
    previous = {"id": "r1", "status": "failed", "question": "更正：集体合同已经签订。", "material_ids": [],
                "answer": "未完成的模型断言", "result": {"citations": [{"ref": "依据1"}],
                "questions": [{"question": "模型假设"}]}, "state": {}}
    run = {"id": "r2", "material_ids": []}
    history, _ = conversation_context({"materials": [], "runs": [previous, run]}, run)
    assert history == [{"turn_id": "r1", "user_statement": "更正：集体合同已经签订。"}]


def test_initial_material_window_is_versioned_and_retained_without_a_fake_tool_step():
    material = {"id": "v1", "filename": "facts.txt", "parsed_text": "不在自贸区。" + "甲" * 15000}
    preview = read_material_page(material_package([material]), 0)
    state = {"material_preview": preview, "steps": []}
    passages = material_passages(state, [material])
    assert passages[0]["text"].startswith("不在自贸区。")
    assert len(passages[0]["text"]) < len(material["parsed_text"])
    assert preview["next_read_request"]["offset"] == 12000
    assert material_passages(state, [{**material, "id": "v2"}]) == []
    previous = {"id": "r1", "status": "completed", "question": "此前问题", "material_ids": ["v1"],
                "state": state, "answer": "此前回答", "result": {}}
    run = {"id": "r2", "material_ids": ["v1"]}
    _, inherited = conversation_context({"materials": [material], "runs": [previous, run]}, run)
    assert inherited == passages
