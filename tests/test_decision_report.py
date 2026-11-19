from __future__ import annotations

import hashlib
import re

from pypdf import PdfReader

from law_agent.review.report_data import build_ai_review, build_legal_sources
from law_agent.review.report_markdown import partition_report_content
from law_agent.review.reports import (
    AIReviewSummary,
    DecisionReportData,
    LegalSource,
    RemediationDetail,
    generate_decision_report,
    verify_report_hash,
    write_decision_report,
)
from law_agent.review.result_builder import LLMReviewResultDraft, inject_citation_markers
from law_agent.review.schemas import GroundedClaim


def report_data() -> DecisionReportData:
    return DecisionReportData(
        case_number="CASE-2026-001",
        decision="附条件通过",
        material_hashes=("a" * 64, "b" * 64),
        legal_sources=(
            LegalSource("个人信息保护法", "第三十八条"),
            LegalSource("Data Export Rules", "Article 5"),
        ),
        remediation_items=("签署标准合同", "留存个人信息保护影响评估"),
        approver="审核人张三",
        approved_at="2026-08-18T12:00:00+08:00",
    )


def test_report_is_deterministic_and_hash_is_verifiable() -> None:
    first = generate_decision_report(report_data())
    second = generate_decision_report(report_data())

    assert first.pdf_bytes == second.pdf_bytes
    assert first.sha256 == hashlib.sha256(first.pdf_bytes).hexdigest()
    assert verify_report_hash(first.pdf_bytes, first.sha256)
    assert not verify_report_hash(first.pdf_bytes + b"changed", first.sha256)


def test_report_contains_required_audit_fields(tmp_path) -> None:
    destination = tmp_path / "decision.pdf"
    artifact = write_decision_report(report_data(), destination)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(destination).pages)

    assert destination.read_bytes() == artifact.pdf_bytes
    assert len(PdfReader(destination).pages) == 2
    assert "CASE-2026-001" in text
    assert "a" * 64 not in text
    assert "sha256" not in text.lower()
    assert "个人信息保护法" in text
    assert "第三十八条" in text
    assert "签署标准合同" in text
    assert "审核人张三" in text
    assert "2026-08-18 12:00:00+08:00" in text


def test_report_surfaces_case_specific_ai_review_and_action_details(tmp_path) -> None:
    data = DecisionReportData(
        case_number="CASE-2026-002",
        case_title="NimbusCRM 境外 SaaS 上线前审查",
        decision="附条件通过",
        material_hashes=("c" * 64,),
        selected_path="个人信息出境标准合同路径",
        manual_confirmation_items=("法务确认标准合同与影响评估均已完成",),
        legal_sources=(LegalSource("个人信息保护法", "第三十八条"),),
        remediation_items=("完成影响评估",),
        remediation_details=(
            RemediationDetail(
                title="完成并签署个人信息保护影响评估",
                description="上线前完成评估并留存签署版",
                owner_role="个人信息保护负责人",
                priority="高优先级",
                required_before="上线前",
                status="待完成",
                evidence_expected="签署版影响评估与版本号",
            ),
        ),
        ai_review=AIReviewSummary(
            risk_level="medium",
            decision_summary=(
                "当前材料支持附条件通过，建议采用标准合同路径；上线前必须完成影响评估、"
                "合同签署备案并补齐分处理者信息。"
            ),
            conclusion="建议附条件通过，先完成影响评估和标准合同闭环。",
            missing_information=("尚未提供分处理者清单",),
            recommended_actions=("补齐分处理者名称、处理地点和变更通知期",),
            risk_boundaries=("接收方或数据类型变化时需要重新审查",),
            business_activity="企业客户支持与工单协作",
            overseas_recipient="NimbusCRM Inc.",
            processing_purpose="提供客户关系管理和售后支持",
            data_types=("姓名", "工作邮箱", "工单内容"),
        ),
        approver="审核人李四",
        approved_at="2026-08-18T13:00:00+08:00",
    )
    destination = tmp_path / "rich-decision.pdf"
    artifact = write_decision_report(data, destination)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(destination).pages)

    assert destination.read_bytes() == artifact.pdf_bytes
    assert "NimbusCRM 境外 SaaS 上线前审查" in text
    assert "建议附条件通过" in text
    assert "审查摘要" in text
    assert "当前材料支持附条件通过" in text
    assert "完整审查意见" in text
    assert "尚未提供分处理者清单" in text
    assert "个人信息保护负责人" in text
    assert "签署版影响评估与版本号" in text
    assert "c" * 64 not in text


def test_report_renders_markdown_review_without_literal_markup_or_forced_wraps(tmp_path) -> None:
    data = DecisionReportData(
        case_number="CASE-2026-003",
        decision="附条件通过",
        material_hashes=("d" * 64,),
        legal_sources=(LegalSource("促进和规范数据跨境流动规定", "第八条"),),
        remediation_items=(),
        ai_review=AIReviewSummary(
            risk_level="medium",
            conclusion=(
                "### 风险定性\n\n该场景涉及**个人信息跨境提供**，存在中等合规风险。\n"
                "企业拟采购境外 CRM / AI SaaS，向欧洲供应商\n"
                "传输客户联系人和工单数据。\n\n"
                "### 关键法律依据\n\n依据《促进和规范数据跨境流动规定》第八条②，\n"
                "自当年1月1日起累计人数达到门槛时，应当订立标准合同。\n\n"
                "### 合规义务与缺口\n"
                "- 需确认累计向境外提供的个人信息主体人数；\n"
                "- 需开展个人信息保护影响评估。"
            ),
        ),
        approver="审核人王五",
        approved_at="2026-08-19T10:00:00+08:00",
    )
    destination = tmp_path / "markdown-review.pdf"

    write_decision_report(data, destination)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(destination).pages)

    assert "###" not in text
    assert "**" not in text
    assert "\\" not in text
    assert "风险定性" in text
    assert "关键法律依据" in text
    assert "合规义务与缺口" in text
    assert "自当年1月1日起" in text.replace("\n", "")


def test_report_data_keeps_complete_review_conclusion() -> None:
    conclusion = "### 风险定性\n" + "完整审查内容。" * 400
    case = {"response": {"review_result": {"risk_level": "medium", "conclusion": conclusion}}}

    ai_review = build_ai_review(case)

    assert ai_review is not None
    assert ai_review.conclusion == conclusion
    assert not ai_review.conclusion.endswith("…")


def test_long_review_conclusion_can_span_pages_without_losing_the_end(tmp_path) -> None:
    conclusion = (
        "### 风险定性\n审查结论起始标记。"
        + "这是需要完整保留的审查结论内容。" * 400
        + "审查结论结束标记。"
    )
    data = DecisionReportData(
        case_number="CASE-2026-LONG",
        decision="附条件通过",
        material_hashes=(),
        legal_sources=(),
        remediation_items=(),
        ai_review=AIReviewSummary(risk_level="medium", conclusion=conclusion),
        approver="审核人赵六",
        approved_at="2026-08-19T11:00:00+08:00",
    )
    destination = tmp_path / "long-review.pdf"

    write_decision_report(data, destination)
    reader = PdfReader(destination)
    text = "".join(page.extract_text() or "" for page in reader.pages).replace("\n", "")

    assert len(reader.pages) > 3
    assert "审查结论起始标记" in text
    assert "审查结论结束标记" in text


def test_report_lists_only_articles_supporting_final_claims() -> None:
    case = {"response": {"review_result": {
        "claims": [{"text": "须履行出境告知义务", "supporting_citation_refs": ["法源-01"]}],
        "citations": [
            {"citation_ref": "法源-01", "title": "个人信息保护法", "article_no": "第三十九条", "usage": "legal_basis", "source_url": "https://example.com/law"},
            {"citation_ref": "法源-02", "title": "办事指南", "article_no": "", "usage": "implementation_reference", "source_url": "https://example.com/guide"},
        ],
    }}}

    sources = build_legal_sources(case)

    assert [(item.title, item.article) for item in sources] == [("个人信息保护法", "第三十九条")]
    assert sources[0].application == "须履行出境告知义务"


def test_citations_stay_in_independent_list_items_and_table_cells():
    report = (
        "### 不应添加引用的标题\n\n"
        "- **人数确认后才可选择路径。**\n"
        "- 尚未提供接收方资料，应先补充核实。\n\n"
        "| 条件 | 本案情况 | 判断 |\n| --- | --- | --- |\n"
        "| 场景 A \\| B | 人数尚未明确 | 不能确定适用路径，需先确认人数。 |\n"
        "| 第二场景 | 已提供材料 | 仍须判断相关适用条件。 |\n"
    )
    texts = ["人数确认后才可选择路径。", "尚未提供接收方资料，应先补充核实。",
             "不能确定适用路径，需先确认人数。", "仍须判断相关适用条件。"]
    claims = [GroundedClaim(text=text, supporting_chunk_ids=[f"c{i}"]) for i, text in enumerate(texts)]
    marked = inject_citation_markers(report, claims, {f"c{i}": f"法源-{i}" for i in range(4)})
    assert marked.count('data-citation-ref=') == 4
    assert re.search(r'路径。\*\*<sup[^>]*data-citation-ref="法源-0"', marked)
    assert re.search(r'核实。<sup[^>]*data-citation-ref="法源-1"', marked)
    assert re.search(r'人数。<sup[^>]*data-citation-ref="法源-2">③</sup> \|', marked)
    assert re.search(r'条件。<sup[^>]*data-citation-ref="法源-3">④</sup> \|', marked)
    assert marked.splitlines()[0] == report.splitlines()[0]
    assert '| --- | --- | --- |' in marked
    assert '| 场景 A \\| B | 人数尚未明确 |' in marked


def test_citation_not_attached_to_table_header_or_ambiguous_cells():
    report = '| 关键适用条件尚未确认 | 判断 |\n| --- | --- |\n| 同一个待核实问题 | 同一个待核实问题 |'
    claims = [GroundedClaim(text=text, supporting_chunk_ids=['c1']) for text in
              ['关键适用条件尚未确认', '同一个待核实问题']]
    assert inject_citation_markers(report, claims, {'c1': '法源-01'}) == report


def test_generic_usage_notes_do_not_swallow_substantive_qualifications():
    note = '本结论基于当前材料及已召回证据，不构成正式法律意见。'
    condition = '本报告不构成正式法律意见，且未评估境外法律。'
    body, boundaries, usage = partition_report_content('### 判断\n\n**条件尚未确认。**\n\n**' + note + '**', (note, condition))
    assert body == '### 判断\n\n**条件尚未确认。**'
    assert boundaries == (condition,)
    assert usage == (note,)


def test_writing_contract_is_shared_by_agent_finish_and_result_generation():
    description = LLMReviewResultDraft.model_json_schema()['properties']['conclusion']['description']
    assert 'Markdown三级小标题' in description
    assert '条件与结论一起强调' in description
    assert '不超过三列' in description
    assert 'HTML标记由程序处理' in description


def test_pdf_data_keeps_full_summary_points_and_boundaries():
    summary = '完整摘要。' * 60
    boundary = '**需要保留的条件**。' * 90
    result = build_ai_review({'response': {'review_result': {
        'decision_summary': summary, 'trigger_reasons': ['**人数未确认**。'],
        'risk_boundaries': [boundary], 'conclusion': '结论正文。',
    }}})
    assert result.decision_summary == summary
    assert result.risk_boundaries == (boundary,)
    assert result.trigger_reasons == ('**人数未确认**。',)


def test_pdf_renders_multipage_table_and_all_report_sections(tmp_path):
    table = '| 核对条件 | 本案情况 | 当前判断 |\n| --- | --- | --- |\n'
    table += '\n'.join(f'| 条件 {i} | **尚未确认** | 需补充材料后判断。 |' for i in range(65))
    data = DecisionReportData(
        case_number='RICH-TEXT-FIXTURE', case_title='固定报告富文本验证', decision='待核实',
        material_hashes=(), legal_sources=(), remediation_items=(), approver='测试审核人',
        approved_at='2026-10-26T10:00:00+08:00',
        ai_review=AIReviewSummary(
            risk_level='insufficient_evidence', decision_summary='人数尚未确认，暂不能确定出境路径。',
            trigger_reasons=('关键前提**尚未确认**。',),
            conclusion='### 适用条件比较\n\n' + table + '\n\n### 个案判断\n\n**条件成立后才可确定路径。**<sup class="cite-marker" data-citation-ref="法源-01">①</sup>\n\n末尾完整保留标记。',
            risk_boundaries=('仅适用于当前材料。', '本报告不构成正式法律意见。'),
        ),
    )
    destination = tmp_path / 'rich-text.pdf'
    write_decision_report(data, destination)
    pages = PdfReader(destination).pages
    text = '\n'.join(page.extract_text() or '' for page in pages)
    assert len(pages) > 3
    for value in ['判断要点', '适用条件比较', '条件 64', '末尾完整保留标记', '结论适用前提与范围', '使用说明']:
        assert value in text
    assert text.count('核对条件') > 1
    assert '**' not in text
    assert '| ---' not in text
