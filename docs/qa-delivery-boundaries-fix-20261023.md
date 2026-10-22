# QA 历史、时点与交付边界修复

本轮只修确定性工程问题。模型、推理强度、提示词、检索策略、事实 Schema、调查工具数量均不变，无付费请求，无新核验、反思或答案改写阶段。保留已有工作区改动及全部历史运行记录。

## 修改及复现

1. **失败轮用户原话**：`qa_context.conversation_context()` 保留当前轮之前所有已提交用户消息。只有正常完成、且材料版本仍被选中的模型回答/追问/引用进入模型历史。失败轮部分回答及引用不继承；仍可复用经过原文校验且版本仍选中的已读材料。新增“用户更正后模型失败”的测试，修改前丢失更正，修改后仅保留原话。
2. **时点引用资格**：`source_citations()` 使用已有 `evidence_as_of()` 逐次计算当前回答时点资格。模型状态更新、流式引用、最终注册表及交付统一使用该资格；不适用来源的 `can_cite_clause=false`、`kind=context`。`render_qa_citations()` 同样限制已标记不可用的来源，并在引用旁显示“非回答时点有效依据，仅供比较”。原文及稳定标记不删除，切回适用时点可恢复资格，不修改已取得原始证据。前端原文窗口显示比较身份和回答日期。
3. **限制及生成状态**：结果保存 `budget_exhausted`、`evidence_gaps`、`generation_status`。主动 finish 的缺口与执行上限导致的限制均保留；引用修补不再把限制清空。QAStore 将工作线程失败或租约超时后的生成状态标为 failed，保留部分正文及已有调查限制。前端仅有实际限制时显示，并将完成文案改为“已回答 · 存在限制”。
4. **中断正文**：`QAAnswer` 接收 incomplete 状态，失败后不渲染普通 Markdown、引用按钮或来源列表。默认闭合的 details 保留可查看的纯文本草稿，提示可能缺少条件及例外。正常流式输出保持原状，历史后台正文未删除。
5. **正式审查安全收口**：system_abstention 不构造 SemanticVerdict，返回 `semantic_grounding={status:not_run, reason:system_abstention}`。已执行核验的 supported/unsupported/uncertain 接口保持不变。事实自检允许无核验记录；工作流把 not_run 视为未完成，证据不足及审批阻断仍保留。

失败用户历史、旧来源资格、虚假 supported 三项先用测试复现失败后修改。既有时点比较测试以前明确允许不可用来源保留法律引用资格，现已收紧；历史比较仍能完成，不一律拒绝旧来源。预算和中断相关既有测试继续保留，并扩充最终结果断言及失败/租约超时回归。

## 验证

- 后端全套：1119 passed in 69.12s。
- 随后补充时点流式/最终标注、恢复资格测试；包含这些新增用例的针对性测试：121 passed in 10.17s。
- 前端：25 项测试通过，包括真实组件服务端渲染，断言失败草稿默认关闭、HTML 被转义、普通 Markdown 不渲染，以及正常流式 Markdown 不受影响。使用现有 Vite、React 和 Node 测试依赖，没有新增依赖。
- `tsc && vite build`、Ruff、`git diff --check` 通过。
- 无真实模型、Judge 或联网检索调用，未重跑人数与 HR 法律题。

这些改动保证身份、引用资格和交付状态按程序契约传递；不能保证模型正文没有错误法律推导。非适用来源可作为明确标记的比较资料保留，程序没有通过自然语言判断某一句是否正确运用了历史法规。历史保存结果不迁移、不重写。前端此次验证为组件渲染和构建，未执行浏览器端到端视觉验收。

## 本轮文件

生产后端：`qa_context.py`、`qa_agent.py`、`qa_citations.py`、`qa_store.py`、`agent_tools.py`、`workflow.py`。

前端：`QAAnswer.tsx`、`ComplianceQAPage.tsx`、`types/qa.ts`、`styles/qa.css`，以及 package.json 增加现有 Node 测试入口。

回归：`test_qa_context.py`、`test_qa.py`、`test_qa_retrieval.py`、`test_qa_citations.py`、`test_review_completion_contracts.py`、`QAAnswer.test.ts`。
