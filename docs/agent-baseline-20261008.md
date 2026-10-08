# Production Agent 首轮候选基线（2026-10-08）

## 当前结论

首轮建立了 8 个合成候选，并使用当前生产 `agent_runtime.execute_agent_task`、真实模型及 Elasticsearch + pgvector 检索运行。5 个案例形成交付，其中 4 个完成 judge 评分：3 个候选 PASS、1 个候选 FAIL。另 1 个已交付但 judge 调用失败，3 个运行未完成，均被模型服务的 `HTTP 402 / Insufficient Balance` 阻断，计入 UNEVALUATED。

**这不是已经人工审定的 Golden 基线，也不能报告为 75% 法律正确率。** 所有案例均为 `candidate`，不是实际客户案件；首轮 judge 与受测 Agent 同为 `deepseek-flash`，且首轮 judge 输入包含生产 trace。后续评测工具已改为只给最终交付及其引用证据，排除生产语义判词与未引用检索结果；本批旧评分没有重新调用模型复评。

生产 Agent 的实现、提示词、预算及门禁均未修改。对照运行 manifest 中的散列，`agent.py`、`agent_runtime.py`、`agent_tools.py`、`web_research.py`、`semantic_grounding.py` 内容保持一致。评测专用工具补充了候选标记、允许/禁止的法律判断、原文引用输入、逐步落盘和确定性服务阻断的单独归类。

## 逐案状态

| 案例 | 观察 | 当前评分 | 证据边界 |
| --- | --- | --- | --- |
| `eval_cross_border_008` | 起初形成标准合同或认证的路径，反复遭语义门禁拒绝后预算耗尽，最后 abstain | FAIL | 原材料相对时间/约数与冻结 intake 精确口径的衔接需要复核；存在后续引用错误，不能只归因为门禁过严 |
| `eval_standard_contract_003` | 覆盖跨境 HR 免予路径、并列条件及剩余义务 | PASS（候选） | 单独同意要求是否忽略法定非同意处理基础，需人工复核 |
| `eval_cross_border_001` | 没有将日均50万用户换算为出境门槛，披露缺口后 abstain | PASS（候选） | 没有实际追问；预设回答未消费 |
| `eval_shanghai_001` | 对具体地域、行业、数据类型与清单范围未确认的案件保留判断 | PASS（候选） | 地方实体规则和报告附带判断仍待人工核查 |
| `agent_material_conflict_001` | 报告指出12万与120万矛盾，保留不同数量下的条件化路径 | UNEVALUATED | 最终交付已保存，judge 因余额不足失败，不认定通过 |
| `eval_sensitive_002` | 已检索并尝试交付，语义校验调用遇到余额不足 | UNEVALUATED | 不评价未完成的敏感信息法律判断 |
| `agent_historical_001` | 首次模型调用被余额不足阻断 | UNEVALUATED | 历史时点能力尚未测出 |
| `eval_out_of_corpus_001` | 首次模型调用被余额不足阻断 | UNEVALUATED | 本轮未验证库外法域 abstain 能力 |

完整输入、评分与生产拒绝记录见[逐案报告](../data/review_runs/agent_core_20261008_round1/report.md)，机器可读结果见[summary.json](../data/review_runs/agent_core_20261008_round1/summary.json)，运行身份与生产散列见[manifest.json](../data/review_runs/agent_core_20261008_round1/manifest.json)。原始 HTTP 402 结果曾被框架列为 FAIL，已离线改列 UNEVALUATED；旧结果保留在对应 `result.raw.json` 以及 `summary.raw.json`、`status.raw.json`，没有改题或重新评审来覆盖旧分数。修订说明见[assessment_update.json](../data/review_runs/agent_core_20261008_round1/assessment_update.json)。

## 主要 bad case：可判断门槛最后未能交付

`eval_cross_border_008` 的目标限定为出境机制，不是判断整个业务已经合规。冻结 intake 提供非 CIIO、非重要数据、当年普通个人信息累计12万人、敏感信息0人和排除免予情形的合成事实。Agent 第6步的交付尝试已经按现行规则识别标准合同或认证路径。

之后的语义校验多次认定结论不确定。拒绝原因包括：原材料的“过去一年约12万名”与 intake 中的“当年累计去重12万人”是否一致、免予排除事实是否经过核实，以及目的地、影响评估、单独同意、合同或认证状态等是否确认。部分理由可能混合了“出境机制选择”与“业务整体合规”的审查范围；但 Agent 自身也持续把已补充的口径列为待核实事项，后续还出现认证办法条款编号与实际证据不匹配。

最终16轮预算耗尽，生产系统以 `insufficient_evidence` 收口。法源已经被检索到，不能将本案简单归为检索零召回。可读失败分类是：**事实 grounding/确认边界、法律路径收口、引用定位、预算耗尽**。需要逐项复核的候选原因是：题目补充事实与原材料衔接不够清晰、生产 intake 的可信身份语义、报告自己引入额外缺口、语义门禁的审查范围、修订过程中新增引用错误。

应先做输入一致性的评测对照：保留本案原记录，另建一题让材料与 intake 都明确使用同一时点、同一统计期、同一去重人数，同时限定问题仍为出境机制。它检验事实 grounding 的具体边界，不通过给生产 Agent 固定搜索/阅读步骤来改善分数。若一致输入仍失败，再单独规划生产修复 Slice。

## PASS 也需要复核

HR 候选的 judge 接受了免予路径，但报告还将单独同意表述为普遍要求。[《个人信息保护法》第十三条](https://www.miit.gov.cn/zwgk/zcwj/flfg/art/2022/art_04a0f1fb5df244e39688fd5372623a8d.html)同时规定法定非同意处理基础。该报告是否机械扩大了同意义务，需要法律人工审定；本轮不能以 judge PASS 消除这个疑点。

首轮没有任何真实 `request_input`，因此 scripted answers 全部未使用。材料不足和材料冲突案例通过直接诚实 abstain 交付，这符合当前 rubric；但不能据此称 `request_input → resume` 的真实模型能力已经覆盖。

本轮也尚未建立 freshness/new-source 受控案例。后续需要保证用于检验阻断的新官方材料确实未进入其受控法源环境；法源留出或固定 Web 输入要单列为受控边界评测，不能冒充自主发现的线上表现。

## 未完成工作与第二轮方向

模型余额不足是明确外部阻断，已经停止真实调用。恢复可用服务后，应先完成历史时点、敏感信息、库外法域的运行，并完成材料冲突案例的 judge 评审；同时用隔离后的 judge 输入复核已有交付。旧记录保留，新增运行使用新目录。

```powershell
python scripts/run_agent_baseline.py --round 1 --workers 1 `
  --case eval_sensitive_002 --case agent_historical_001 --case eval_out_of_corpus_001 `
  --output-dir data/review_runs/agent_core_round1_remaining
```

再根据有效失败补第二批，优先考虑上面的输入一致性对照、免予条件成立/缺失的对照、行业与重要数据的认定边界、实际暂停恢复、freshness/new-source 阻断。总量逐步扩至约15–20个，不在首轮仍不完整时先填满重复题。

法律人工审定仍是成为 Golden 的必要步骤；每题需确认事实可信身份、允许/禁止判断、关键法源与例外，以及哪些追问确实会改变最终判断。

## 本地验证

Agent eval 与 retrieval eval 相关测试共72项通过，相关文件 Ruff 检查和 `git diff --check` 通过。已额外核对保存结果的 schema、运行 manifest 与当前候选输入完全一致、生产文件散列一致，以及改进后的条款覆盖检查与本批原确定性结果一致。余额不足后没有再次调用 Agent 或 judge；第二轮、freshness 和真实暂停恢复均未声称完成。
