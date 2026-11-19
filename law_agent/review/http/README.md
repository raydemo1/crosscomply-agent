# Review HTTP adapters

`law_agent.review.api.create_app` 负责应用组装、存储依赖和认证依赖；路由按业务边界由本目录的 `register_*_routes` 函数挂载。

| 模块 | 作用 |
| --- | --- |
| `auth.py` | 登录、退出、当前会话 |
| `system.py` | 公开健康检查、管理员诊断 |
| `cases.py` | 案件、材料、快照、审查任务、事实抽取 |
| `facts.py` | 申请人回答追问与确认事实 |
| `matter.py` | 案件内问答（含流式） |
| `annotations.py` | 锚定在冻结材料原文上的批注与追审 |
| `revisions.py` | 修改建议与工作稿 |
| `remediation.py` | 整改计划、整改任务、证据与复核 |
| `transfers.py` | 持续出境业务登记及变更影响确认 |
| `activity.py` | 反馈、待办 |
| `qa.py` | 独立合规问答、SSE 事件流、转为案件 |
| `integrations.py` | 飞书审批投递、回调和订阅 |
| `reports.py` | PDF 决策报告下载（首次下载时生成） |
| `knowledge.py` | 知识库管理、导入任务、回收站、补库复核 |
| `templates.py` | 案件模板 |
| `users.py` | 管理员用户管理 |
| `responses.py` | 对外 JSON 响应整形（去掉模型私有工具对话） |
| `schemas.py` | HTTP 请求/响应模型 |

路由只做认证、参数校验、错误映射和响应整形；领域写操作通过 store 或领域模块完成。路由中有阻塞 I/O（数据库、文件解析、模型调用）时，写成普通 `def` 或用 `run_in_threadpool` 包装，不要在 `async def` 中直接调用。
