# 架构

CrossComply 用**一个** Compliance Agent 完成案件调查和报告起草。职责划分如下：

- **Agent** 决定读什么、查什么、何时追问、何时交付；
- **程序** 负责冻结输入、执行工具、限制预算、校验引用、持久化状态，并执行所有对外动作；
- **人** 确认事实、复核报告、发起和完成审批、验收整改、治理知识库。

本文只描述当前实现。

## 运行组件

```mermaid
flowchart LR
    UI["React 工作台"] --> API["FastAPI<br/>law_agent.review.api"]
    API --> DB[("PostgreSQL")]
    API -->|排队 ReviewTask / QA run| DB
    W["Worker<br/>law_agent.review.worker"] -->|租约领取| DB
    W --> RT["agent_runtime.execute_agent_task"]
    RT --> AG["Compliance Agent<br/>review.agent"]
    AG -->|检索| ES["Elasticsearch<br/>关键词"]
    AG -->|检索| PGV["pgvector<br/>向量"]
    AG -->|受限 Web 调查| EXA["Exa（仅官方来源）"]
    KW["knowledge-worker<br/>law_agent.kb.enrichment_worker"] -->|补库核验、飞书提醒| DB
    API --> S3[("MinIO<br/>材料原件、PDF 报告")]
    API <-->|审批投递 / 验签事件| FS["飞书"]
```

| 进程 | 入口 | 职责 |
| --- | --- | --- |
| API | `law_agent.review.api:app` | HTTP 接口、认证、领域写操作；只排队任务，不直接跑 Agent |
| Worker | `python -m law_agent.review.worker` | 依次领取审查任务和问答任务，调用 runtime，回写结果 |
| knowledge-worker | `python -m law_agent.kb.enrichment_worker` | 处理 Agent 提交的补库来源，发送飞书待办提醒 |

检索必须同时连通 ES 与 pgvector，任一不可用即显式失败，没有本地回退；LLM 节点也没有规则兜底。

## 审查流程

1. 申请人创建案件、上传材料、确认申报事实。
2. 发起审查时，程序冻结 `MaterialSnapshot`（材料版本与解析文本）和 `IntakeSnapshot`（确认事实），排队一个 `ReviewTask`。
3. Worker 领取任务，Agent 循环决策，直到交付、暂停追问或预算耗尽。
4. 交付的报告依次经过确定性引用门禁和独立语义校验；不通过则带着反馈退回 Agent 修改。
5. Worker 写回结果和整改动作清单，并按结果推进案件状态。
6. 审查人复核后发起飞书审批；验签后的审批事件写回终态，需要时生成带 SHA-256 的 PDF 决策报告。

## Agent 的动作

每一轮模型输出一个 `AgentDecision`（`law_agent/review/agent.py`）JSON，由程序执行其中的单个动作。没有固定步骤顺序。

| 动作 | 作用 |
| --- | --- |
| `propose_plan` | 更新对用户可见的计划，不暂停运行 |
| `read_material` / `read_materials` | 分页读取冻结材料；单次最多返回 48,000 UTF-8 字节，XLSX 可按工作表读取 |
| `grep_materials` | 在冻结材料中字面搜索，返回命中位置和上下文 |
| `run_python` | 在 WASI 沙箱中处理材料数据（需配置 `LAWAGENT_WASI_PYTHON_ROOT`） |
| `search_evidence` | 1–4 个查询的受控混合检索，每批最多返回 10 段原文 |
| `read_evidence` | 1–4 个读取请求：按条号、chunk 或来源目录精读已检索到的来源 |
| `search_web` | 在受控库不足或需核实时效时检索官方来源；结果只作调查上下文，不能作为引用证据 |
| `queue_enrichment` | 把新官方来源提交补库，单次运行最多 2 条；入库前没有引用资格 |
| `request_input` | 持久化暂停，向申请人提问，可附结构化 `fact_questions` |
| `finish` | 提交结构化报告草稿，进入门禁 |

默认预算（`AgentState`）：16 轮、5 次检索、10 次读取、5 次 Web 搜索、2 条补库。预算耗尽只能以 `insufficient_evidence` 收口。

### 检索

- 每个查询内部先融合关键词和向量结果（各取前 50 候选），日期过滤在融合前完成；再跨查询按最强命中选择来源，重复命中不额外加分。
- 排序只按案件地域、行业和查询用途做软加权（`retrieval/boosts.py`）：条件性地方/行业规范在地域或行业匹配时加权、地域明确不符时降权；不按"正式法条 / 解释辅助"角色固定升降。
- 未返回正文的高排名位置以 `candidate_navigation` 形式给出定位和短预览；预览不能被引用，需要精读后才进入证据。
- 额外 rerank 默认关闭（`RERANK_MODE=off`）。

### 上下文

`agent_context.compact_evidence_context` 无损整理发送给模型的证据：同源元数据合并、与正文相同的全文不重复发送。checkpoint、引用门禁和最终证据账本不做裁剪或摘要。

## 程序保证

- **主体累计冻结**：`review.portfolio` 按主体和自然年汇总持续业务的已签署审批基线，展示单项最大值和未去重合计。不把过期、无法解析或被人工排除的记录计作零。入队时排除本案所属业务，恢复时读取原快照。跨档提示由法务确认并写入案件事件。
- **法规变更影响**：审查 Worker 完成任务后索引实际引用；知识库发布前后由 `kb.change_detection` 比较条文和日期元数据，`review.impact` 匹配当前审批基线和待审批报告。阻断级开放标记拦截飞书送审，法务可说明理由后忽略或发起业务复审；审批回写后独立结案，不改写原报告或原审批。

- **冻结输入不可变**：快照只能新增版本，Agent 无法修改；补充材料改变事实时必须重新冻结。
- **引用门禁**（`agent_tools.ComplianceAgentTools.finalize`）：法律主张只能由 `can_cite_clause=true` 的 chunk 支持；材料引用必须属于本次快照且原文唯一精确命中；非证据不足的结论至少有一条可引用法条；引用只来自实际返回给 Agent、且在审查时点已发布生效的证据。
- **独立语义校验**（`semantic_grounding.SemanticGroundingVerifier`）：独立模型对照冻结事实、材料原文、事实账本和被引法条，核验每条主张、主结论、路径、问题分类、建议和风险边界。地域/行业不适用判 unsupported，必要事实不足判 uncertain；任何逐项不通过都不能被总体通过掩盖。正式核验模型由 `LAWAGENT_SEMANTIC_MODEL` 指定。
- **时效阻断**：发现可能改变核心结论的新官方材料（`web_impact=core`）时设置 `freshness_hold`，案件不能进入审批。
- **对外动作不属于 Agent**：Agent 不能发飞书、不能批准、不能激活整改。
- **终态权威**：审批终态只能由验签且幂等的飞书事件写入（`workflow.py`）。

未通过校验的报告保存在 `AgentState.report_draft`，与反馈一起进入下一轮决策，通过后清空。

## 事实来源

- `AgentState.fact_ledger` 每条记录 `field / value / source_type / source_ref / status`。新任务从 `IntakeSnapshot` 写入 `confirmed_intake / confirmed`。`confirmed` 只表示申请人确认了填报内容，不等于独立核实。
- Agent 不能通过工具改写事实或其可信等级。
- `request_input` 的回答记为 `applicant_statement / unverified`；审查人的回答只是操作指引，不进入事实账本。
- 申请人点击"确认这些事实并继续审查"时，服务器在**一个事务**内写回 intake、新增事实快照、关闭旧的等待任务并排队新任务（`fact_confirmation.py`、`transactions.py`），任何一步失败都整体回滚。仅限 `needs_info` 状态的案件。

## 人工职责

| 角色 | 职责 |
| --- | --- |
| 申请人（requester） | 填报并确认事实，提供材料，回答追问，处理分派的整改 |
| 审查人（reviewer） | 核对证据与引用、要求补正、发起飞书审批、验收整改 |
| 审批人 | 在飞书完成通过 / 附条件通过 / 拒绝 |
| 管理员（admin） | 用户管理、知识库治理、补库来源复核 |

## 状态机

案件（`law_agent/review/workflow.py`）：

```text
draft ⇄ needs_info → pending_review → review_running
review_running → needs_info | pending_source_verification | pending_feishu_approval | run_failed
pending_source_verification → review_running
pending_feishu_approval → approved | conditionally_approved | rejected   （仅飞书事件）
run_failed → review_running | needs_info | pending_source_verification | pending_feishu_approval
```

证据不足、仍有缺失信息或存在时效阻断的结果不会被推进到审批。

审查任务（`ReviewTask`，`enterprise_store.py`）：`queued → running → waiting_input → (重新排队) → succeeded | failed | superseded`。领取时获得 30 分钟租约，每次 checkpoint 续约；租约过期的任务在下次领取时被重新排队，并从最近 checkpoint 继续。checkpoint 保存完整 `AgentState` 和面向用户的步骤记录，不保存模型私有推理。

## 案件问答与独立问答

- **案件问答**（`matter_agent.py`）：绑定当前任务和快照版本，只读冻结材料、事实账本、已通过校验的报告及其引用证据；回答同样经过引用门禁和语义校验。不改写报告、事实或任务，只新增 `matter_agent_turn` 事件；上下文超过 120,000 字符时明确拒绝。新事实只能作为待确认建议，由申请人确认后进入上面的冻结与重审流程。
- **合规问答**（`qa_agent.py`、`qa_store.py`）：不依附案件，与审查共用检索和材料工具，在同一 Worker 进程中处理，经 SSE 流式返回；不经过语义核验链路，可转为正式案件。

## 模型配置

主 Agent 与案件问答使用 `LAWAGENT_AGENT_REASONING_EFFORT`，语义校验使用 `LAWAGENT_SEMANTIC_REASONING_EFFORT`（默认均为 low）；其余结构化节点使用 `OPENAI_COMPATIBLE_REASONING_EFFORT`（默认 none）。DeepSeek 思考模式不支持强制指定工具，因此 `strict_tool` 输出模式必须配 none。完整变量见 [配置参考](configuration.md)。

## 代码地图

| 模块 | 职责 |
| --- | --- |
| `review/agent_runtime.py` | 生产审查 runtime：为一个 ReviewTask 组装 Agent、工具和门禁 |
| `review/agent.py`、`agent_tools.py`、`agent_context.py` | 决策循环、预算、受控工具、finalize 门禁、上下文整理 |
| `review/semantic_grounding.py`、`report_grounding.py` | 独立语义校验、报告字段定位 |
| `review/worker.py` | Worker 主循环与成功/暂停/失败回写 |
| `review/enterprise_store.py`、`case_store.py` | 任务、快照、案件、用户持久化（各有内存实现供测试） |
| `review/fact_confirmation.py`、`transactions.py` | 事实确认的原子业务流程 |
| `review/matter_agent.py`、`qa_agent.py` | 案件问答、合规问答 |
| `review/remediation.py`、`feishu.py`、`reports.py` | 整改、飞书审批、PDF 决策报告 |
| `review/work_inbox.py`、`transfer_store.py`、`work_reminders.py` | 待办、持续出境业务、飞书提醒 |
| `review/retrieval/` | 混合检索、融合、加权、时效过滤、来源定位 |
| `review/api.py` + `review/http/` | 应用组装与按业务划分的路由（见 [http/README](../law_agent/review/http/README.md)） |
| `review/service.py` | 检索 benchmark 使用的固定流水线，**不是**生产执行路径 |
| `kb/` | 知识库入库、版本切换、补库 Worker |
| `data/` | 离线数据治理流水线，见 [数据治理](data-governance.md) |
