# Review HTTP adapters

`law_agent.review.api.create_app` 只负责应用组装、存储依赖和认证依赖；路由按业务边界由本目录的注册函数挂载。

| 模块 | 作用 |
| --- | --- |
| `auth.py` | 登录、退出、当前会话 |
| `system.py` | 健康检查 |
| `knowledge.py` | 法律法规/规章制度知识库管理、导入任务、回收站 |
| `cases.py` | 案件、材料、快照、审查任务 |
| `remediation.py` | 整改计划、整改任务、证据和提交审核 |
| `integrations.py` | 飞书审批投递、回调和订阅 |
| `reports.py` | 决策报告生成与下载 |
| `activity.py` | 反馈、案件事件、仪表盘摘要 |
| `templates.py` | 案件模板 |
| `users.py` | 管理员用户管理 |
| `evaluation.py` | 评测任务、状态与结果缓存 |
| `schemas.py` | HTTP 请求/响应模型 |

路由适配器只做认证、参数校验、错误映射和响应整形；领域写操作通过存储或知识库服务完成，不启动 CLI 子进程。
