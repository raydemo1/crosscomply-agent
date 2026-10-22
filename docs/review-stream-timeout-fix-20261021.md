# 正式核验内部流式与超时修复

2026-10-21。落实 `review-timeout-diagnosis-20261021.md` 中已复现的读取超时问题。不修改审查提示、事实与来源、核验规则、Agent 执行预算或模型配置，不扩跑原首批之外的用例。

## 已实施

- 核心核验、建议／问题发现核验和风险边界核验统一启用内部 SSE。接收思考数据用于执行观测，不保存为核验结果或展示给用户；收齐 `finish_reason=stop` 和 `[DONE]` 后才解析最终 JSON。
- 断流、缺少结束标记、长度截断、工具调用、API 错误、不合法 SSE 或完整但错误的 JSON 都不会被当成核验通过。原有 Pydantic Schema、法律主张覆盖、栏目覆盖、状态聚合和审批门禁继续执行。
- HTTPX 负责连接和读取空闲超时；AnyIO 总时限负责取消异步网络请求。清理连接时屏蔽取消，确保关闭过程完成，不采用仅停止等待、请求仍在后台挂起的做法。同步核验入口通过 BlockingPortal 调用，也可从已有事件循环所在的线程调用。
- 默认连接超时仍使用现有 `OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60`；新增读取空闲 `OPENAI_COMPATIBLE_READ_TIMEOUT_SECONDS=60`、单次核验请求总时限 `OPENAI_COMPATIBLE_TOTAL_TIMEOUT_SECONDS=120`。总时限约束一次请求，现有核验节点最多一次重试不变，不是无限延长整个任务。
- 传输／断流等调用失败重试时保留原消息，不再给模型追加“上一轮 JSON 解析失败”。真正的 JSON、Schema 和业务覆盖错误保留对应纠错反馈；严格工具参数的 JSON 错误也保留明确类型，避免误归类成网络错误。
- usage 增加响应头、首响应字节、首事件、首思考、首正文、最长数据间隔与失败阶段记录，不记录私有思考正文。未使用新接收路径的调用不伪造这些阶段数据。

实现复用现有 HTTPX，并显式将 HTTPX、AnyIO 声明为运行依赖；没有更换 SDK、增加兼容回退或备用核验路径。普通问答及原生工具调查没有切换到这条结构化核验接收路径。

有关库的语义依据：[HTTPX 连接／读取超时](https://www.python-httpx.org/advanced/timeouts/)、[AnyIO 总时限与取消](https://anyio.readthedocs.io/en/stable/cancellation.html)、[同步线程调用异步任务](https://anyio.readthedocs.io/en/stable/threads.html)。行为以本地实际测试为准。

## 原失败请求的真实复测

冻结 HR 失败的 `model-call-007.request.json`，使用改后的生产 `SemanticGroundingVerifier.node`。实际 HTTPX 请求逻辑体与原请求完全相同，仅增加 `stream` 和 `stream_options`；没有缩减资料、修改提示、增加输出上限或关闭思考。

| 指标 | 修复前的原失败记录 | 修复后本次复测 |
|---|---|---|
| 完成情况 | 两次读取超时，未取得完整结果 | 一次完成，无重试 |
| 耗时 | 60.313 秒、60.297 秒 | **67.500 秒** |
| 首响应头／首事件 | 旧记录未观测 | 约 1.609 秒 |
| 首思考数据 | 旧记录未观测 | 约 2.500 秒 |
| 首正文 | 未取得 | 约 65.906 秒 |
| 最长数据间隔 | 旧记录未观测 | 约 0.891 秒 |
| JSON 与核心覆盖检查 | 未到达 | 通过 |

本次实际生成仍超过 60 秒，首次正文也在 60 秒之后；持续接收的思考／事件数据使读取没有空闲超时，随后完整收齐结果。因此本次直接证明了读取行为修复，不宣称模型生成本身已变快。

本次思考为 12,849 token，总输出 13,225 token；输入 22,424 token，缓存命中 22,272。核验原始状态为 `supported`，但相同请求此前诊断曾出现 `uncertain`，不能把这次传输成功或状态值当成法律可靠性改进。

仅复测核心核验请求及其 Schema／核心覆盖契约，没有继续整案调查、报告修订、其他栏目核验或审批。原首批仍保留“3 条交付、1 条运行失败”的历史结果；剩余 6 条未启动。

## 验证

定向测试覆盖：

- 思考持续到达时，生成时间可以超过读取空闲时限，思考不进入返回 JSON。
- 无数据到达时触发读取空闲超时；持续保活也不能绕过总时限；本地 HTTP 服务确认超时后连接实际关闭。
- 流式分片、保活、结束后的 usage、完整结束标记、错误与截断处理。
- 传输重试输入不变、重试上限不变；JSON、严格工具参数、Schema 错误仍有正确反馈。
- 三类生产核验节点均启用流式，原核验上下文和覆盖检查回归。

原失败请求复测后，还补充了严格工具参数 JSON 异常分类的回归测试；未改变上述已验证的流式接收路径。最终后端全量 **918 passed**，相关 Ruff 与 `git diff --check` 通过。本轮没有前端改动。

## 文件与证据

- `law_agent/llm/structured_stream.py`：内部接收、取消和完成校验。
- `law_agent/llm/openai_compatible.py`、`law_agent/review/llm.py`：结构化调用入口及分类重试。
- `law_agent/review/semantic_grounding.py`：三类核验节点启用 SSE。
- `law_agent/config.py`、`.env.example`、`pyproject.toml`：超时配置与现有依赖声明。
- `law_agent/llm/usage.py`：阶段观测。
- `data/review_runs/review_stream_fix_20261021/`：冻结方案、源码／请求指纹、实际请求、原始 SSE、usage、结果和核心契约检查。源码在真实复测运行期间保持不变。
