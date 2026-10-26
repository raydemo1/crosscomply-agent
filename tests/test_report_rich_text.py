import re

from pypdf import PdfReader

from law_agent.review.report_data import build_ai_review
from law_agent.review.report_markdown import partition_report_content
from law_agent.review.reports import AIReviewSummary, DecisionReportData, write_decision_report
from law_agent.review.result_builder import LLMReviewResultDraft, inject_citation_markers
from law_agent.review.schemas import GroundedClaim


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
    for value in ['判断要点', '适用条件比较', '条件 64', '末尾完整保留标记', '结论适用边界', '使用说明']:
        assert value in text
    assert text.count('核对条件') > 1
    assert '**' not in text
    assert '| ---' not in text
