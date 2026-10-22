# 正式审查提交契约简化（2026-10-22）

## 已实施

模型只填写需要判断的内容，重复字段由程序组装。正式报告、派单和审批输出字段保持原有结构；没有增加核验调用或改变推理强度。

| 原模型输入 | 现模型输入 | 程序职责 |
|---|---|---|
| missing_information + issues.unknowns重复问题原文 | questions + issues.question_indexes | 从唯一问题清单生成报告缺口与问题项unknowns |
| missing_answer_types + issues.answer_type | questions中每题的answer_type | 生成问题到回答类型的映射；单问题项继承该题类型 |
| web_impact + material_web_urls | web_impacts，每项url和impact成对 | 汇总最重要影响和URL列表；无记录即none及空列表 |
| claims.supporting_citation_refs | 不再向模型暴露 | 根据实际证据分组统一生成引用编号 |
| 每个问题项都必填空材料、空法条列表 | 无内容时可省略 | 默认空列表；legal_gap仍须通过材料与正式法条门禁 |

提交报告的顶层字段从15个减至13个，问题项字段从8个减至7个。更主要的减少是问题原文和回答类型不再在多个栏目重复生成，而不是仅减少键名数量。

## 示例

以下是完整的证据不足提交示例。它只演示结构，不是法律判断样本：

```json
{
  "risk_level": "insufficient_evidence",
  "decision_summary": "本次材料尚未明确实际累计出境人数，无法据此确定适用路径；应补充该事实后结合已经取得的法源继续审查。",
  "conclusion": "目前不能仅凭日均处理量确定累计出境人数。",
  "questions": [
    {"text": "本年度实际累计出境的去重自然人数是多少？", "answer_type": "count"}
  ],
  "issues": [
    {
      "kind": "missing_information",
      "title": "累计人数尚未确认",
      "finding": "材料只给出了日均处理量。",
      "question_indexes": [0],
      "recommended_action": "确认实际累计出境人数。"
    }
  ],
  "web_impacts": []
}
```

question_indexes从0开始，仅允许实际存在且不重复的整数下标；待确认和材料冲突问题必须关联至少一题。同一题可关联多个问题项，仍只从questions生成一条事实确认任务。问题文本仅做首尾空白处理和精确去重，不猜测不同表达是否同义。

web_impacts最多2条，不接受同一规范化URL重复登记。总体影响按core、execution_detail、supplement排序汇总；来源资格仍由本轮真实网页发现决定。core影响仍要求证据不足且没有确定法律路径，程序不会自动改写风险等级或正文掩盖矛盾。

## 契约同步

- Agent主提示和finish实际Schema使用questions、question_indexes、web_impacts。
- 核验输入使用同一份草稿及Schema，问题文本反馈定位为questions:0:text；问题关联反馈定位为issues:0:question_indexes:0。
- 问题关联位置的反馈文本为实际下标，完整问题文本可从同一输入questions取得，不把下标字段伪装成原文段落。
- finalizer解析关联后再生成原有ReviewResult；派单继续只使用最终missing_information及其回答类型映射。
- 正式报告与案件解释问答的模型法律主张只包含文本和证据片段编号，程序结果再增加引用编号。
- QA Agent源码未改，也未增加核验步骤。

## 验证和限制

全量pytest：1089项通过。相关文件Ruff及git diff --check通过。

新增测试覆盖实际finish Schema、真实生产组装路径的离线测试、编号越界/重复/布尔值、空问题及精确重复、业务与法源缺口分离、同题多问题项唯一派单、混合回答类型、网页成对字段及汇总、URL去重、核心影响审批阻断、核验反馈定位，以及模型不能填写程序引用编号。离线测试中的模型与核验返回使用固定测试数据，不称为真实模型效果评测。

本轮付费模型调用为0。仅证明重复填写已在结构上移除、程序契约一致，不据此声称法律理解或真实调用成本已经改善。

旧草稿输入协议不再接受，不提供兼容转换。旧评测文件原样保留；依赖旧草稿结构的严格回放测试已退出当前协议测试，改为历史协议拒绝检查及当前原生finish、核验反馈与派单测试。历史法律结论和评分边界没有重写。

生产Schema、源码散列、QA隔离与验证记录位于data/review_runs/targeted_review_qa_20261022/submission_simplification_audit.json。
