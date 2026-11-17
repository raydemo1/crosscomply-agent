# 正式审查小规模实测

本轮使用合成材料、仓库的冻结法源和当前模型。案例标签由项目自身维护，法律结论自动评分另有原文复核记录。每组正式审查每案运行一次。

## 正式审查与固定检索

四个案例分别检查身份冲突、人力资源管理条件不明、机制与手续完成的区别、内部授权的法律效力。两组使用 `deepseek-flash` 生成，经过同一个 `deepseek-v4-pro` 生产核验器，用同一个 `deepseek-flash` Judge 评分。固定组只用一批两条查询的混合检索，至多按核验反馈原地修订一次。Agent 可以继续调查并追问。

| 方法 | 自动评分通过 | 生成及生产核验请求 | 已记录 token | 全程用时中位数，含评测 Judge |
| --- | ---: | ---: | ---: | ---: |
| 当前 Agent | 4/4 | 21 | 401,824 | 127.4 秒 |
| 固定检索 | 2/4 | 11 | 161,861 | 98.3 秒 |

固定组在身份冲突题保留了条件判断，却额外产生法源待核验阻断。客户数据题缺少必要法源，未给出已有事实可支持的机制。Agent 对身份冲突报告的主机制给出两条分支，风险边界仍将出现敏感信息直接概括为触发安全评估。HR 案的边界也写出了过宽的单独同意义务。这两处均出现在自动评分通过的结果中。

两组各只有四次，Agent 的生成及核验 token 约为固定组的 2.48 倍。本轮证据适合展示自主补查如何弥补特定检索漏项，以及为此支付的成本。各组原始结果位于 `data/review_runs/pilot_20261116/fixed_vs_agent/agent/` 和 `data/review_runs/pilot_20261116/fixed_corrected/fixed/`。前一个目录的固定组早期记录有评测脚本参数错误，表中使用纠正参数后完成的实跑。

## 核验器实验

| 数据 | 现有核验 | 候选核验 | 决定 |
| --- | --- | --- | --- |
| 四组成对短稿 | 拦截并定位 4/4 错误稿，4/4 对照稿通过 | 加强提示拦截并定位 4/4 错误稿，2/4 对照稿通过 | 保留现有核验 |
| 四份实际报告的原始交付稿 | 两处指定风险边界均漏过，两份对照通过 | 专查风险边界的提示找出 1/2 个指定错误，另一处仍漏过。两份对照中一份通过、一份超时 | 继续保留现有核验 |

加强版退回对照稿的原因包括要求补充独立的报告分支，以及要求把官方解释加入正式法条的 `supporting_chunk_ids`。相关原判词保存在逐案 JSON。长稿回放复用原始交付稿、当次取得的证据、材料及事实账本。同一输入对比两种提示。输出位于 `verifier_before`、`verifier_focused`、`verifier_observed_before` 和 `verifier_observed_narrow`。

## 补正与送审

案件详情和审批发起、重试共用后端送审判断。报告首屏显示当前状态和阻断理由，跳到事实、法源、送审版本或审批入口。新材料或事实快照使旧审查结果无法继续投递。

真实模型首次实跑时，申报人已在新快照中确认不存在出境。Agent 看到旧材料写「送审时尚未核对」，将其解释成事实冲突并再次追问。加入解释新旧事实关系的指引后，同案重新跑过暂停、确认和旧快照拒绝，确认后到达报告核验阶段。此时模型供应商返回 HTTP 402，真实模型流程停在该处。同期两案补正指引回归中，身份冲突案自动评分通过，另一案也受到供应商阻断。运行记录保存在 `approval_flow`、`approval_flow_retest` 和 `runtime_after`。

随后在隔离的 PostgreSQL、真实 FastAPI 路由、MinIO 和生产 Worker 持久化代码上回放同案。回放使用记录中的真实暂停状态及可控报告结论。本地审批回调通过项目验签逻辑，业务数据库只写入一次终态。结果记录在 `approval_flow_replay/flow.json`。

| 检查 | 观察到的结果 |
| --- | --- |
| 申报人补充前尝试审批 | HTTP 409，投递次数为 0 |
| 提交和明确确认事实 | 新建事实快照，原等待任务被替换 |
| 重复提交旧快照 | HTTP 409 |
| 送审后重复发起 | 返回同一审批实例，投递次数为 1 |
| 无效签名与重复签名回调 | 无效签名 HTTP 401，有效回调写入已批准，重复回调标记为 duplicate，终态事件为 1 条 |

浏览器使用该回放的 API 记录验证桌面和手机的可送审与审批结束入口，并用待补事实的响应验证暂缓送审入口。

## 通用工具比较

计划中的直接模型基线使用 DeepSeek Pro 和固定组的同一证据包。四案请求均收到 HTTP 402，未产生可评分结果。ChatGPT 或 Claude 的产品级 Research 对照仍待实跑。三份合成案例输入与独立评分约束已保存在 `data/review_runs/pilot_20261116/research_inputs/`，可交给任一可用 Research 会话，保持材料、审查日期和问题一致。

## 复跑入口

```powershell
python scripts/eval_report_verifier.py --output-dir data/review_runs/verifier_new
python scripts/eval_review_pilot.py --output-dir data/review_runs/pilot_new
python scripts/probe_approval_flow.py --output-dir data/review_runs/flow_new
python scripts/probe_approval_flow.py --replay-from data/review_runs/pilot_20261116/approval_flow_retest/flow.json --output-dir data/review_runs/flow_replay_new
```

前三条调用模型服务。回放命令只需要本地服务和一份已记录的暂停状态。
