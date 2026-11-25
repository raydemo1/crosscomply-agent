from law_agent.data.schemas import SourceRecord
from law_agent.kb.source_draft import draft_source


def test_stated_fields_win_and_the_rest_is_inferred_with_evidence(tmp_path) -> None:
    path = tmp_path / "rule.txt"
    path.write_text("汽车数据安全管理若干规定（试行）\n国家互联网信息办公室\n交通运输部\n令\n第7号\n"
                    "第一条 为了规范汽车数据处理活动。\n第二条 适用范围。\n第三条 定义。\n本规定自2021年10月1日起施行。",
                    encoding="utf-8")
    base = SourceRecord(source_id="user_x", title="汽车数据安全管理若干规定（试行）", source_url="file:///rule.txt",
                        source_site="local_import", doc_type="guideline", file_format="txt")
    draft = draft_source(path, base=base, explicit={"issuing_body": "国家互联网信息办公室等五部门"}, published=[],
                         client=None)

    assert draft.source.doc_type == "regulation" and draft.source.authority == "departmental_rule"
    assert draft.source.citation_role == "conditional_industry_basis"  # 汽车：行业性依据
    assert draft.source.effective_date == "2021-10-01"
    assert draft.source.issuing_body == "国家互联网信息办公室等五部门"
    assert draft.evidence["issuing_body"] == "导入时填写" and "令" in draft.evidence["doc_type"]
    assert any(chunk.can_cite_clause for chunk in draft.prepared.chunks)
    # Without a model only the rules read it, and a person confirms before it is published.
    assert draft.reader == "rules" and draft.needs_review


def test_an_operator_who_states_the_classification_needs_no_model(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("内部参考\n第一条 内容。", encoding="utf-8")
    base = SourceRecord(source_id="user_y", title="内部参考", source_url="file:///note.txt",
                        source_site="local_import", doc_type="guideline", file_format="txt")
    stated = {"doc_type": "guideline", "authority": "ministry_policy", "citation_role": "implementation_reference",
              "issuing_body": "国家互联网信息办公室", "publish_date": "2026-01-01"}

    class Unused:
        def chat_json(self, _messages):
            raise AssertionError("nothing left for the model to read")

    draft = draft_source(path, base=base, explicit=stated, published=[], client=Unused())
    assert draft.reader == "operator" and not draft.needs_review
    assert draft.source.authority == "ministry_policy"
