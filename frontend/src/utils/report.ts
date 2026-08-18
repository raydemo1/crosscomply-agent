/**
 * Report exporter — generate Markdown and HTML review reports from a saved
 * case and trigger a browser download.
 *
 * Both formats are self-contained and human-readable, covering the full
 * review chain: question, material, facts, query plan, evidence
 * self-check, conclusion, recommended actions, risk boundaries, and the
 * governed citations (with chunk text and source URLs).
 *
 * The HTML variant inlines a minimal stylesheet matching the Trust &
 * Authority palette so the exported file renders correctly when opened
 * standalone or attached to an email.
 */

import type { CaseIntake, ReviewFacts, CitationGroup, RetrievalHit, RetrievalQuery } from '../types/api';
import { isReviewFailedResponse } from '../types/api';
import type { SavedCase } from '../types/case';
import { CASE_STATUS_LABELS } from './workflow';
import {
  CITATION_ROLE_LABELS,
  AUTHORITY_LABELS,
  DOC_TYPE_LABELS,
  EVIDENCE_ISSUE_LABELS,
  EVIDENCE_STATUS_LABELS,
  LAW_STATUS_LABELS,
  QUERY_TYPE_LABELS,
  RISK_LABELS,
  USAGE_LABELS,
  USAGE_ORDER,
  formatTime,
  renderBool,
  renderList,
  renderText,
} from './display';

// ---------------------------------------------------------------------------
// Markdown generation
// ---------------------------------------------------------------------------

function mdEscape(text: string): string {
  // Minimal escaping for safe inline rendering.
  return text.replace(/\\/g, '\\\\').replace(/\|/g, '\\|');
}

function factsToMarkdown(facts: ReviewFacts): string {
  const rows: Array<[string, string]> = [
    ['业务活动', renderText(facts.business_activity)],
    ['数据类型', renderList(facts.data_types)],
    ['敏感个人信息', renderBool(facts.sensitive_personal_info)],
    ['跨境传输', renderBool(facts.cross_border_transfer)],
    ['境外接收方', renderText(facts.overseas_recipient)],
    ['处理目的', renderText(facts.processing_purpose)],
    ['法律依据/同意', renderText(facts.legal_basis_or_consent)],
    ['行业', renderText(facts.industry)],
    ['地区', renderText(facts.region)],
    ['缺失信息', renderList(facts.missing_information)],
  ];
  const header = '| 字段 | 值 |\n| --- | --- |';
  const body = rows.map(([k, v]) => `| ${mdEscape(k)} | ${mdEscape(v)} |`).join('\n');
  return `${header}\n${body}`;
}

function queriesToMarkdown(queries: RetrievalQuery[] | undefined): string {
  if (!queries || queries.length === 0) return '_未生成检索查询_';
  const header = '| # | 类型 | 查询文本 |\n| --- | --- | --- |';
  const body = queries
    .map((q, i) => `| ${i + 1} | ${QUERY_TYPE_LABELS[q.query_type] ?? q.query_type} | ${mdEscape(q.text)} |`)
    .join('\n');
  return `${header}\n${body}`;
}

function chunksById(hits: RetrievalHit[] | undefined): Map<string, RetrievalHit> {
  const map = new Map<string, RetrievalHit>();
  if (hits) for (const h of hits) map.set(h.chunk_id, h);
  return map;
}

function citationsToMarkdown(
  groups: CitationGroup[],
  chunks: Map<string, RetrievalHit>,
): string {
  if (groups.length === 0) return '_无可引用证据_';
  const ordered = [...groups].sort(
    (a, b) => USAGE_ORDER.indexOf(a.usage) - USAGE_ORDER.indexOf(b.usage),
  );
  return ordered
    .map((group) => {
      const head = `### ${USAGE_LABELS[group.usage]}（${group.citations.length} 条）`;
      const scope = group.scope_note ? `\n\n_范围：${mdEscape(group.scope_note)}_` : '';
      const items = group.citations
        .map((c) => {
          const lines: string[] = [
            `- **${mdEscape(c.citation_ref || '未编号')} · ${mdEscape(c.citation_label ?? c.title)}**`,
            `   - 支持关系：见结论逐句依据中的 ${mdEscape(c.citation_ref || '未编号')}`,
            `   - 法源类型：${mdEscape(DOC_TYPE_LABELS[c.doc_type] ?? c.doc_type)} · 权威等级：${mdEscape(AUTHORITY_LABELS[c.authority] ?? c.authority)}`,
            `   - 引用角色：${CITATION_ROLE_LABELS[c.citation_role] ?? '引用角色未提供'}`,
            `   - 法律状态：${mdEscape(LAW_STATUS_LABELS[c.law_status] ?? '状态未知')} · 发布机关：${mdEscape(c.issuing_body || '未提供')}`,
            `   - 发布日期：${mdEscape(c.publish_date || '未提供')} · 生效日期：${mdEscape(c.effective_date || '未提供')}`,
            `   - 官方原文：${c.source_url ? mdEscape(c.source_url) : '未提供'}`,
          ];
          if (c.full_article_text) {
            lines.push(`   - 条文内容：`);
            lines.push('');
            lines.push('     > ' + mdEscape(c.full_article_text).replace(/\n/g, '\n     > '));
          } else {
            lines.push('   - 条文内容：当前知识库未收录完整条文');
          }
          return lines.join('\n');
        })
        .join('\n');
      return `${head}${scope}\n\n${items}`;
    })
    .join('\n\n');
}

function claimsToMarkdown(
  claims: { text: string; supporting_citation_refs: string[] }[],
): string {
  if (claims.length === 0) return '';
  const lines = ['**结论逐句依据：**'];
  claims.forEach((claim, index) => {
    lines.push(`${index + 1}. ${mdEscape(claim.text)}`);
    const refs = claim.supporting_citation_refs ?? [];
    lines.push(`   - 支持法源：${refs.length > 0 ? refs.map(mdEscape).join('、') : '—'}`);
  });
  return lines.join('\n');
}

function requireResponse(saved: SavedCase): NonNullable<SavedCase['response']> {
  if (!saved.response) throw new Error('案件尚未生成审查结果，无法导出报告。');
  return saved.response;
}

function caseStatusLabel(status: SavedCase['status']): string {
  return CASE_STATUS_LABELS[status];
}

function intakeToMarkdown(intake: CaseIntake): string {
  const rows: Array<[string, string]> = [
    ['业务活动', intake.business_activity || '待补充'],
    ['数据类型', renderList(intake.data_types)],
    ['敏感个人信息', renderBool(intake.sensitive_personal_info)],
    ['跨境传输', renderBool(intake.cross_border_transfer)],
    ['重要数据识别', intake.important_data_status],
    ['关基运营者状态', intake.ciio_status],
    ['非敏感个人信息数量', intake.annual_non_sensitive_count || '待补充'],
    ['敏感个人信息数量', intake.annual_sensitive_count || '待补充'],
    ['境外接收方', intake.overseas_recipient || '待补充'],
    ['目的地', intake.destination_region || '待补充'],
    ['处理目的', intake.processing_purpose || '待补充'],
    ['出境路径', intake.transfer_mechanism || '待判断'],
    ['供应商', intake.vendor_name || '待补充'],
    ['合同状态', intake.contract_status || '待补充'],
    ['法律依据/同意', intake.legal_basis_or_consent || '待补充'],
  ];
  const header = '| 字段 | 人工确认值 |\n| --- | --- |';
  const body = rows.map(([key, value]) => '| ' + mdEscape(key) + ' | ' + mdEscape(value) + ' |').join('\n');
  return header + '\n' + body;
}

function actionsToMarkdown(saved: SavedCase): string {
  if (saved.actions.length === 0) return '_暂无服务端整改动作_';
  return saved.actions.map((action, index) => {
    const due = action.due_date ? '，截止 ' + action.due_date : '';
    const state = action.status === 'completed' ? '已完成' : action.status === 'in_progress' ? '处理中' : '待处理';
    const description = action.description ? '：' + mdEscape(action.description) : '';
    return (index + 1) + '. **' + mdEscape(action.title) + '**（' + state + '，负责人角色：' + mdEscape(action.owner_role) + due + '）' + description;
  }).join('\n');
}

function knowledgeSnapshotToMarkdown(response: NonNullable<SavedCase['response']>): string {
  if (isReviewFailedResponse(response)) return '_审查失败，未生成知识库快照_';
  const titles = new Set<string>();
  for (const packet of response.source_evidence_packets ?? []) titles.add(packet.title);
  for (const group of response.citation_groups ?? []) {
    for (const citation of group.citations) titles.add(citation.title);
  }
  return titles.size > 0 ? [...titles].map((title) => '- ' + mdEscape(title)).join('\n') : '_未记录来源快照_';
}

export function buildMarkdownReport(saved: SavedCase): string {
  const { question, materialText, feedback, savedAt } = saved;
  const response = requireResponse(saved);
  const lines: string[] = [];

  lines.push('# CrossComply 跨境数据合规案件报告');
  lines.push('');
  lines.push('- 生成时间：' + formatTime(savedAt));
  lines.push('- 案件状态：' + caseStatusLabel(saved.status));
  lines.push('- 案件编号：' + saved.id);
  if (!isReviewFailedResponse(response)) {
    lines.push(`- 追踪编号：\`${response.trace_id}\``);
  }
  lines.push(`- 风险等级：${isReviewFailedResponse(response) ? '审查失败' : RISK_LABELS[response.review_result.risk_level]}`);
  lines.push('');
  lines.push('---');
  lines.push('');
  lines.push('## 一、审查问题');
  lines.push('');
  lines.push(question);
  lines.push('');
  lines.push('## 二、待审查材料');
  lines.push('');
  lines.push('```text');
  lines.push(materialText);
  lines.push('```');
  lines.push('');

  if (isReviewFailedResponse(response)) {
    lines.push('## 三、审查失败');
    lines.push('');
    lines.push(`- 失败节点：${mdEscape(response.failed_node)}`);
    lines.push(`- 原因：${mdEscape(response.reason)}`);
    lines.push(`- 详情：${mdEscape(response.message)}`);
    lines.push(`- 重试次数：${response.attempts}`);
    return lines.join('\n');
  }

  const result = response.review_result;
  const facts = response.review_facts;
  const selfCheck = response.evidence_self_check;
  const chunks = chunksById(response.evidence_chunks);

  lines.push('## 三、人工确认事实');
  lines.push('');
  lines.push(intakeToMarkdown(saved.intake));
  lines.push('');
  lines.push('## 四、AI 提取事实摘要');
  lines.push('');
  lines.push(factsToMarkdown(facts));
  lines.push('');

  lines.push('## 五、检索查询计划');
  lines.push('');
  lines.push(queriesToMarkdown(response.retrieval_queries));
  lines.push('');

  lines.push('## 六、证据自检');
  lines.push('');
  lines.push(`- 状态：**${EVIDENCE_STATUS_LABELS[selfCheck.status]}**`);
  lines.push(`- 是否触发二次检索：${selfCheck.second_retrieval_triggered ? '是' : '否'}`);
  if (selfCheck.triggered_reasons.length > 0) {
    lines.push(`- 触发原因：${selfCheck.triggered_reasons.map(mdEscape).join('；')}`);
  }
  if (selfCheck.issues.length > 0) {
    lines.push('');
    lines.push('**检出问题：**');
    selfCheck.issues.forEach((issue) => {
      lines.push(`- ${EVIDENCE_ISSUE_LABELS[issue.issue_type] ?? issue.issue_type}：${mdEscape(issue.description)}`);
    });
  }
  if (selfCheck.second_retrieval_plan) {
    const plan = selfCheck.second_retrieval_plan;
    lines.push('');
    lines.push('**二次检索计划：**');
    lines.push(`- 扩展查询数：${plan.expanded_queries.length}`);
    lines.push(`- 增加 top_k：${plan.increased_top_k}`);
    lines.push(`- 加强 boost：${plan.stronger_boost ? '是' : '否'}`);
    lines.push(`- 原因：${mdEscape(plan.reason)}`);
  }
  lines.push('');

  lines.push('## 七、审查结论');
  lines.push('');
  lines.push(`**风险等级：${RISK_LABELS[result.risk_level]}**`);
  lines.push('');
  lines.push(result.conclusion);
  lines.push('');
  const groundedClaims = claimsToMarkdown(result.claims);
  if (groundedClaims) {
    lines.push(groundedClaims);
    lines.push('');
  }

  if (result.trigger_reasons.length > 0) {
    lines.push('## 八、触发原因');
    lines.push('');
    result.trigger_reasons.forEach((r) => lines.push(`- ${mdEscape(r)}`));
    lines.push('');
  }

  if (result.recommended_actions.length > 0) {
    lines.push('## 九、建议动作');
    lines.push('');
    result.recommended_actions.forEach((a, i) => lines.push(`${i + 1}. ${mdEscape(a)}`));
    lines.push('');
  }

  if (result.risk_boundaries.length > 0) {
    lines.push('## 十、风险边界');
    lines.push('');
    result.risk_boundaries.forEach((b) => lines.push(`- ${mdEscape(b)}`));
    lines.push('');
  }

  if (result.missing_information.length > 0) {
    lines.push('## 十一、缺失信息');
    lines.push('');
    result.missing_information.forEach((m) => lines.push(`- ${mdEscape(m)}`));
    lines.push('');
  }

  lines.push('## 十二、服务端整改动作');
  lines.push('');
  lines.push(actionsToMarkdown(saved));
  lines.push('');
  lines.push('## 十三、法规知识库快照');
  lines.push('');
  lines.push(knowledgeSnapshotToMarkdown(response));
  lines.push('');
  lines.push('## 十四、可引用证据');
  lines.push('');
  lines.push(citationsToMarkdown(response.citation_groups, chunks));
  lines.push('');

  if (feedback) {
    lines.push('---');
    lines.push('');
    lines.push('## 人工反馈');
    lines.push('');
    if (feedback.conclusionUseful !== null) {
      lines.push(`- 结论评价：${feedback.conclusionUseful ? '有用' : '无用'}`);
    }
    const verdicts = Object.entries(feedback.citationVerdicts);
    if (verdicts.length > 0) {
      lines.push('- 引用评价：');
      verdicts.forEach(([cid, v]) => lines.push(`  - \`${cid}\`：${v === 'correct' ? '正确' : '错误'}`));
    }
    if (feedback.missingSources) lines.push(`- 缺少来源：${mdEscape(feedback.missingSources)}`);
    if (feedback.notes) lines.push(`- 备注：${mdEscape(feedback.notes)}`);
    lines.push('');
  }

  lines.push('---');
  lines.push('');
  lines.push('> 本报告由 CrossComply 自动生成，仅供研究参考，不构成正式法律意见。');

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// HTML generation
// ---------------------------------------------------------------------------

function escHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const HTML_STYLE = `
body{font-family:'Source Sans 3','Noto Sans SC','Microsoft YaHei',system-ui,sans-serif;background:#f8fafc;color:#0f172a;line-height:1.6;max-width:880px;margin:0 auto;padding:40px 24px;}
h1{font-family:'Source Serif 4','Noto Serif SC',Georgia,serif;color:#1e3a8a;border-bottom:3px solid #b45309;padding-bottom:8px;}
h2{font-family:'Source Serif 4','Noto Serif SC',Georgia,serif;color:#1e3a8a;margin-top:32px;border-left:4px solid #b45309;padding-left:10px;}
h3{font-family:'Source Serif 4','Noto Serif SC',Georgia,serif;color:#1e40af;margin-top:20px;}
.meta{background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:16px;margin:16px 0;font-size:14px;}
.meta div{margin:2px 0;}
.risk{display:inline-block;padding:3px 12px;border-radius:999px;font-weight:700;font-size:13px;}
.risk-high{background:rgba(185,28,28,.1);color:#b91c1c;border:1px solid rgba(185,28,28,.35);}
.risk-medium{background:rgba(180,83,9,.1);color:#b45309;border:1px solid rgba(180,83,9,.35);}
.risk-low{background:rgba(4,120,87,.1);color:#047857;border:1px solid rgba(4,120,87,.35);}
.risk-insufficient_evidence{background:rgba(100,116,139,.1);color:#64748b;border:1px solid rgba(100,116,139,.35);}
.bad{background:rgba(185,28,28,.06);border:1px solid rgba(185,28,28,.35);border-radius:8px;padding:10px 14px;color:#b91c1c;font-weight:600;margin:12px 0;}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;}
th,td{border:1px solid #cbd5e1;padding:8px 10px;text-align:left;}
th{background:#1e3a8a;color:#fff;}
tr:nth-child(even){background:#f1f5f9;}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:8px;overflow-x:auto;font-size:13px;}
blockquote{border-left:4px solid #b45309;background:rgba(180,83,9,.05);margin:10px 0;padding:10px 16px;color:#334155;}
.claim{background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:10px 12px;margin:8px 0;}
.claim-text{font-weight:600;color:#334155;}
.claim-refs{font-size:12px;color:#64748b;margin-top:4px;}
.cite{background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:12px 14px;margin:10px 0;}
.cite-title{font-weight:700;color:#0f172a;}
.cite-meta{font-size:12px;color:#64748b;margin-top:4px;}
.cite-chunk{margin-top:8px;background:#f8fafc;border-left:3px solid #1e3a8a;padding:8px 12px;font-size:13px;white-space:pre-wrap;}
a{color:#1e40af;}
.muted{color:#64748b;}
footer{margin-top:40px;padding-top:16px;border-top:1px solid #cbd5e1;font-size:12px;color:#94a3b8;}
`.trim();

function factsToHtml(facts: ReviewFacts): string {
  const rows: Array<[string, string]> = [
    ['业务活动', renderText(facts.business_activity)],
    ['数据类型', renderList(facts.data_types)],
    ['敏感个人信息', renderBool(facts.sensitive_personal_info)],
    ['跨境传输', renderBool(facts.cross_border_transfer)],
    ['境外接收方', renderText(facts.overseas_recipient)],
    ['处理目的', renderText(facts.processing_purpose)],
    ['法律依据/同意', renderText(facts.legal_basis_or_consent)],
    ['行业', renderText(facts.industry)],
    ['地区', renderText(facts.region)],
    ['缺失信息', renderList(facts.missing_information)],
  ];
  const body = rows
    .map(([k, v]) => `<tr><th>${escHtml(k)}</th><td>${escHtml(v)}</td></tr>`)
    .join('');
  return `<table><tbody>${body}</tbody></table>`;
}

function intakeToHtml(intake: CaseIntake): string {
  const rows: Array<[string, string]> = [
    ['业务活动', intake.business_activity || '待补充'],
    ['数据类型', renderList(intake.data_types)],
    ['敏感个人信息', renderBool(intake.sensitive_personal_info)],
    ['跨境传输', renderBool(intake.cross_border_transfer)],
    ['重要数据识别', intake.important_data_status],
    ['关基运营者状态', intake.ciio_status],
    ['非敏感个人信息数量', intake.annual_non_sensitive_count || '待补充'],
    ['敏感个人信息数量', intake.annual_sensitive_count || '待补充'],
    ['境外接收方', intake.overseas_recipient || '待补充'],
    ['目的地', intake.destination_region || '待补充'],
    ['处理目的', intake.processing_purpose || '待补充'],
    ['出境路径', intake.transfer_mechanism || '待判断'],
    ['供应商', intake.vendor_name || '待补充'],
    ['合同状态', intake.contract_status || '待补充'],
    ['法律依据/同意', intake.legal_basis_or_consent || '待补充'],
  ];
  const body = rows.map(([key, value]) => '<tr><th>' + escHtml(key) + '</th><td>' + escHtml(value) + '</td></tr>').join('');
  return '<table><tbody>' + body + '</tbody></table>';
}

function queriesToHtml(queries: RetrievalQuery[] | undefined): string {
  if (!queries || queries.length === 0) return '<p class="muted">未生成检索查询</p>';
  const rows = queries
    .map((q, i) => `<tr><td>${i + 1}</td><td>${escHtml(QUERY_TYPE_LABELS[q.query_type] ?? q.query_type)}</td><td>${escHtml(q.text)}</td></tr>`)
    .join('');
  return `<table><thead><tr><th>#</th><th>类型</th><th>查询文本</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function citationsToHtml(
  groups: CitationGroup[],
  chunks: Map<string, RetrievalHit>,
): string {
  if (groups.length === 0) return '<p class="muted">无可引用证据</p>';
  const ordered = [...groups].sort(
    (a, b) => USAGE_ORDER.indexOf(a.usage) - USAGE_ORDER.indexOf(b.usage),
  );
  return ordered
    .map((group) => {
      const items = group.citations
        .map((c) => {
          const chunkHtml = c.full_article_text
            ? `<div class="cite-chunk"><strong>完整条文</strong><br>${escHtml(c.full_article_text)}</div>`
            : '<div class="cite-chunk muted">当前知识库未收录完整条文</div>';
          const link = c.source_url
            ? `<a href="${escHtml(c.source_url)}" target="_blank" rel="noopener">${escHtml(c.source_url)}</a>`
            : '<span class="muted">—</span>';
          return `<div class="cite">
            <div class="cite-title"><a id="${escHtml(c.citation_ref)}"></a>${escHtml(c.citation_ref || '未编号')} · ${escHtml(c.citation_label ?? c.title)}</div>
            <div class="cite-meta">法源类型：${escHtml(DOC_TYPE_LABELS[c.doc_type] ?? c.doc_type)} · 权威等级：${escHtml(AUTHORITY_LABELS[c.authority] ?? c.authority)} · 法律状态：${escHtml(LAW_STATUS_LABELS[c.law_status] ?? '状态未知')}</div>
            <div class="cite-meta">引用角色：${escHtml(CITATION_ROLE_LABELS[c.citation_role] ?? '引用角色未提供')} · 发布机关：${escHtml(c.issuing_body || '未提供')} · 官方原文：${link}</div>
            ${chunkHtml}
          </div>`;
        })
        .join('');
      const scope = group.scope_note
        ? `<p class="muted">范围：${escHtml(group.scope_note)}</p>`
        : '';
      return `<h3>${escHtml(USAGE_LABELS[group.usage])}（${group.citations.length} 条）</h3>${scope}${items}`;
    })
    .join('');
}

function claimsToHtml(
  claims: { text: string; supporting_citation_refs: string[] }[],
): string {
  if (claims.length === 0) return '';
  const items = claims
    .map((claim, index) => {
      return `<div class="claim">
        <div class="claim-text">${index + 1}. ${escHtml(claim.text)}</div>
        <div class="claim-refs">支持法源：${(claim.supporting_citation_refs ?? []).length > 0 ? escHtml((claim.supporting_citation_refs ?? []).join('、')) : '—'}</div>
      </div>`;
    })
    .join('');
  return `<p><strong>结论逐句依据：</strong></p>${items}`;
}

export function buildHtmlReport(saved: SavedCase): string {
  const { question, materialText, feedback, savedAt } = saved;
  const response = requireResponse(saved);
  const failed = isReviewFailedResponse(response);
  const risk = failed ? 'failed' : response.review_result.risk_level;
  const riskLabel = failed ? '审查失败' : RISK_LABELS[response.review_result.risk_level];

  const parts: string[] = [];
  parts.push('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">');
  parts.push('<meta name="viewport" content="width=device-width, initial-scale=1">');
  parts.push(`<title>法律合规审查报告 — ${escHtml(saved.id)}</title>`);
  parts.push(`<style>${HTML_STYLE}</style>`);
  parts.push('</head><body>');
  parts.push('<h1>CrossComply 跨境数据合规案件报告</h1>');

  parts.push('<div class="meta">');
  parts.push(`<div>生成时间：${escHtml(formatTime(savedAt))}</div>`);
  parts.push('<div>案件状态：' + escHtml(caseStatusLabel(saved.status)) + '</div>');
  parts.push('<div>案件编号：<code>' + escHtml(saved.id) + '</code></div>');
  if (!failed) {
    parts.push(`<div>追踪编号：<code>${escHtml(response.trace_id)}</code></div>`);
  }
  parts.push(`<div>风险等级：<span class="risk risk-${escHtml(risk)}">${escHtml(riskLabel)}</span></div>`);
  parts.push('</div>');

  parts.push('<h2>一、审查问题</h2>');
  parts.push(`<p>${escHtml(question)}</p>`);

  parts.push('<h2>二、待审查材料</h2>');
  parts.push(`<pre>${escHtml(materialText)}</pre>`);

  if (failed) {
    parts.push('<h2>三、审查失败</h2>');
    parts.push('<div class="meta">');
    parts.push(`<div>失败节点：${escHtml(response.failed_node)}</div>`);
    parts.push(`<div>原因：${escHtml(response.reason)}</div>`);
    parts.push(`<div>详情：${escHtml(response.message)}</div>`);
    parts.push(`<div>重试次数：${response.attempts}</div>`);
    parts.push('</div>');
    parts.push('<footer>本报告由 CrossComply 自动生成，仅供研究参考，不构成正式法律意见。</footer>');
    parts.push('</body></html>');
    return parts.join('\n');
  }

  const result = response.review_result;
  const facts = response.review_facts;
  const selfCheck = response.evidence_self_check;
  const chunks = chunksById(response.evidence_chunks);

  parts.push('<h2>三、人工确认事实</h2>');
  parts.push(intakeToHtml(saved.intake));
  parts.push('<h2>四、AI 提取事实摘要</h2>');
  parts.push(factsToHtml(facts));

  parts.push('<h2>五、检索查询计划</h2>');
  parts.push(queriesToHtml(response.retrieval_queries));

  parts.push('<h2>六、证据自检</h2>');
  parts.push('<div class="meta">');
  parts.push(`<div>状态：<strong>${escHtml(EVIDENCE_STATUS_LABELS[selfCheck.status])}</strong></div>`);
  parts.push(`<div>是否触发二次检索：${selfCheck.second_retrieval_triggered ? '是' : '否'}</div>`);
  if (selfCheck.triggered_reasons.length > 0) {
    parts.push(`<div>触发原因：${escHtml(selfCheck.triggered_reasons.join('；'))}</div>`);
  }
  parts.push('</div>');
  if (selfCheck.issues.length > 0) {
    parts.push('<p><strong>检出问题：</strong></p><ul>');
    selfCheck.issues.forEach((issue) => {
      parts.push(`<li>${escHtml(EVIDENCE_ISSUE_LABELS[issue.issue_type] ?? issue.issue_type)}：${escHtml(issue.description)}</li>`);
    });
    parts.push('</ul>');
  }

  parts.push('<h2>七、审查结论</h2>');
  parts.push(`<p><span class="risk risk-${escHtml(result.risk_level)}">${escHtml(RISK_LABELS[result.risk_level])}</span></p>`);
  parts.push(`<p>${escHtml(result.conclusion)}</p>`);
  parts.push(claimsToHtml(result.claims));

  let idx = 8;
  if (result.trigger_reasons.length > 0) {
    parts.push(`<h2>${cnNum(idx++)}、触发原因</h2><ul>`);
    result.trigger_reasons.forEach((r) => parts.push(`<li>${escHtml(r)}</li>`));
    parts.push('</ul>');
  }
  if (result.recommended_actions.length > 0) {
    parts.push(`<h2>${cnNum(idx++)}、建议动作</h2><ol>`);
    result.recommended_actions.forEach((a) => parts.push(`<li>${escHtml(a)}</li>`));
    parts.push('</ol>');
  }
  if (result.risk_boundaries.length > 0) {
    parts.push(`<h2>${cnNum(idx++)}、风险边界</h2><ul>`);
    result.risk_boundaries.forEach((b) => parts.push(`<li>${escHtml(b)}</li>`));
    parts.push('</ul>');
  }
  if (result.missing_information.length > 0) {
    parts.push(`<h2>${cnNum(idx++)}、缺失信息</h2><ul>`);
    result.missing_information.forEach((m) => parts.push(`<li>${escHtml(m)}</li>`));
    parts.push('</ul>');
  }

  parts.push('<h2>十二、服务端整改动作</h2>');
  parts.push('<ol>');
  if (saved.actions.length === 0) {
    parts.push('<li class="muted">暂无服务端整改动作</li>');
  } else {
    saved.actions.forEach((action) => {
      const due = action.due_date ? '，截止 ' + escHtml(action.due_date) : '';
      const state = action.status === 'completed' ? '已完成' : action.status === 'in_progress' ? '处理中' : '待处理';
      const description = action.description ? '：' + escHtml(action.description) : '';
      parts.push('<li><strong>' + escHtml(action.title) + '</strong>（' + state + '，负责人角色：' + escHtml(action.owner_role) + due + '）' + description + '</li>');
    });
  }
  parts.push('</ol>');
  parts.push('<h2>十三、法规知识库快照</h2>');
  parts.push('<ul>');
  const snapshotTitles = new Set<string>();
  for (const packet of response.source_evidence_packets ?? []) snapshotTitles.add(packet.title);
  for (const group of response.citation_groups ?? []) {
    for (const citation of group.citations) snapshotTitles.add(citation.title);
  }
  if (snapshotTitles.size === 0) {
    parts.push('<li class="muted">未记录来源快照</li>');
  } else {
    for (const title of snapshotTitles) parts.push('<li>' + escHtml(title) + '</li>');
  }
  parts.push('</ul>');
  parts.push('<h2>十四、可引用证据</h2>');
  parts.push(citationsToHtml(response.citation_groups, chunks));

  if (feedback) {
    parts.push('<h2>人工反馈</h2><div class="meta">');
    if (feedback.conclusionUseful !== null) {
      parts.push(`<div>结论评价：${feedback.conclusionUseful ? '有用' : '无用'}</div>`);
    }
    const verdicts = Object.entries(feedback.citationVerdicts);
    if (verdicts.length > 0) {
      parts.push('<div>引用评价：</div><ul>');
      verdicts.forEach(([cid, v]) => parts.push(`<li><code>${escHtml(cid)}</code>：${v === 'correct' ? '正确' : '错误'}</li>`));
      parts.push('</ul>');
    }
    if (feedback.missingSources) parts.push(`<div>缺少来源：${escHtml(feedback.missingSources)}</div>`);
    if (feedback.notes) parts.push(`<div>备注：${escHtml(feedback.notes)}</div>`);
    parts.push('</div>');
  }

  parts.push('<footer>本报告由 CrossComply 自动生成，仅供研究参考，不构成正式法律意见。</footer>');
  parts.push('</body></html>');
  return parts.join('\n');
}

function cnNum(n: number): string {
  const map = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二', '十三', '十四'];
  return map[n] ?? String(n);
}

// ---------------------------------------------------------------------------
// Download trigger
// ---------------------------------------------------------------------------

function download(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function safeFilename(id: string): string {
  const suffix = id.replace(/[^a-zA-Z0-9_-]/g, '').slice(-12) || 'case';
  const date = new Date().toISOString().slice(0, 10);
  return `lawagent-review-${date}-${suffix}`;
}

export function downloadMarkdown(saved: SavedCase): void {
  download(`${safeFilename(saved.id)}.md`, buildMarkdownReport(saved), 'text/markdown;charset=utf-8');
}

export function downloadHtml(saved: SavedCase): void {
  download(`${safeFilename(saved.id)}.html`, buildHtmlReport(saved), 'text/html;charset=utf-8');
}
