<div align="center">

# CrossComply

**让企业数据出境合规审查可验证、可追问、可审批的开源 Agent 工作台**

*Stateful Compliance Agent · Controlled Legal RAG · Verifiable Citation Gate · Enterprise Governance*

[![CI](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)
![React + Vite](https://img.shields.io/badge/Frontend-React%20%2B%20Vite-61DAFB?logo=react&logoColor=111)
![ES + pgvector](https://img.shields.io/badge/Hybrid%20Search-Elasticsearch%20%2B%20pgvector-005571)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

[核心差异](#一个真实案例看懂差距) · [功能亮点](#核心功能) · [实测数据](#实测数据) · [快速启动](#快速启动) · [技术栈](#技术栈)

</div>

---

## 一个真实案例看懂差距

> **真实企业场景**：某游戏/出海企业拟向境外提供约 25 万名玩家的非敏感个人信息，同时需要确认既有数据出境安全评估的有效期与个人信息保护认证的现行技术标准。

| 对比维度 | 通用大模型（同底座直接作答） | CrossComply Agent 工作台 |
| --- | --- | --- |
| **时效与新旧法** | 把 2024 规定已延长至 **3 年且可申请延长** 的安全评估有效期答成 2022 旧规“2 年、只能重报”；将 2025 年第 53 号公告已替换为 **GB/T 46068** 的认证依据答成已废止的 TC260 指南 | 每轮绑定审查时点 `as_of_date`，自动通过新旧法关联 `later_provisions` 从旧规顺藤摸瓜精读 2025 年 53 号公告与 2024 规定原文 |
| **条号与法源** | 36 题盲测中 **72.2%** 存在法条、条号错误或遗漏，甚至凭记忆编造跨领域审批条例 | 所有引用必须来自本次已取得的有效法条原文，经过**确定性引用门禁 + 独立语义校验**，法律准确性完全无瑕率 **77.8% vs 13.9%（+63.9 pp）** |
| **事实与边界** | 材料字段矛盾或缺少 CIIO / 重要数据确认时强行下确定性结论，产生高危误判 | 证据不足时**持久化暂停追问申请人**，由申请人事务化确认事实后继续，**危险错误率（`wrong_definite`）0.0%** |

---

## 核心功能

### 1. 合规即时问答（Compliance Q&A）
- **开箱即用**：无需建案即可发起合规咨询，支持一次上传多份业务说明与多工作表 Excel 数据清单。
- **自主多轮调查 + SSE 流式溯源**：Agent 自主拆解查询、按条精读、必要时暂停追问，实时流式输出结论，每个 `[依据N]` 均可点击高亮核对法条原文。
- **答后完整性复核**：回答写出后自动检查是否遗漏核心法律路径、豁免例外或子问题，缺漏时**继承原检索域补查并修订定稿**；复杂咨询支持**一键转为正式审查案件**。

### 2. 有状态案件审查（Stateful Case Review）
- **不可变快照冻结**：发起审查即冻结材料版本（`MaterialSnapshot`）、申报事实（`IntakeSnapshot`）与同主体累计台账（`PortfolioSnapshot`），确保调查全程输入不可篡改。
- **10 类受控调查工具**：分页读取材料与 Excel 工作表、`grep_materials` 精确匹配、**WASI Python 沙箱**隔离核算数据规模、受控多域混合检索、按条精读、受限官方 Web 调查、提交在线补库、持久化暂停追问申请人、提交结构化报告草稿。
- **断点续跑与双层门禁**：Worker 依托 **30 分钟租约 + Checkpoint** 实现长任务可恢复执行；报告草稿必须依次通过**确定性引用门禁**（法律效力 + 条款定位 + 原文精确命中 + 时效校验）和**独立语义校验器**（`SemanticGroundingVerifier`），不通过则带诊断退回修订。
- **案件内问答与法务批注**：绑定当前快照与已审报告，支持法务原文批注、修改建议与受控案件问答；新事实经申请人**单事务原子确认**后触发重审。

### 3. 企业级治理与审批闭环（Enterprise Governance）
- **同主体自然年累计台账**：按主体和自然年自动聚合持续业务的已批基线，提前预警逼近 10 万 / 100 万申报门槛的跨档风险。
- **法规变更影响自动标红**：知识库发布新规或版本更新时，自动扫描历史审批基线与待审报告的引用条款，阻断级变更直接拦截送审并提醒法务复审。
- **飞书验签审批 & PDF 决策交付**：Agent 零对外审批权限，飞书审批终态**仅由验签幂等的回调事件写回**；自动生成带 **SHA-256 校验和**的 PDF 决策报告与整改清单，支持整改证据提交验收。
- **在线知识库治理与纠错回流**：Agent 发现的新官方来源进入人工核验补库队列；线上审批与法务纠错经脱敏校验后回流至评测集持续加固。

### 4. 受控法规检索与防泄漏评测（Controlled RAG & Evals）
- **结构化受控语料**：覆盖 **65 份**官方法规、标准、指南与问答（**2,119 个分块、46.9 万字**），支持 DOCX 纵向合并单元格还原、HTML 响应式去重、PDF 跨页表重组与 Docling OCR；负面清单按“分节路径 + 列名 + 单行”切块，确保任意碎片召回仍保留完整语境。
- **引用资格三层解耦**：严格区分“法定条款效力”、“条号定位”与“案件适用性”，问答/指南类辅助来源禁止冒充法定条款引用；在线入库采用 `FULL_TEXT_THRESHOLD = 16_000`“模型优先、规则兜底”策略完成网页正文去噪与新旧法 `precedence_relations` 抽取。
- **查询级多域混合检索与校准重排**：动态生成全国通用 / 地方自贸区（沪京津深、粤港澳大湾区）/ 行业（汽车、工信）Overlay 检索池；ES BM25 + `bge-m3` 经 RRF 融合并由 `bge-reranker-v2-m3` 按 `w = 0.85` 校准重排，消除单查询 min-max 拉伸偏差。
- **内置 78 题防泄漏评测框架**：严格隔离 `dev`（42 题）与散列锁定的 `test`（36 题），支持离线查询回放、多轮条款级召回与跨模型双盲评测。

---

## 实测数据

> 以下全部结果均测自**锁定测试集 `test`（36 题，从未用于调参，36/36 完成）**：

<div align="center">

| **97.2%** (`35/36`) | **+63.9 pp** (`77.8% vs 13.9%`) | **0.0%** (`0/36`) | **90.5%** / **97.2%** |
| :---: | :---: | :---: | :---: |
| 跨模型双盲总通过率 | 法律准确性完全无瑕率提升 | 危险错误率 `wrong_definite` | 多轮必引证据召回 / 引用门禁 |

</div>

### 跨模型双盲回答质量对照（同底座 DeepSeek，独立裁判 `claude-opus-5.5`）

将 `crosscomply_qa` 与同底座裸模型 `direct` 在 36 道 `test` 题上的 **72 条交付匿名打乱并剥离系统标识**，严格按 `agent_rubric` 逐条盲评：

| 评测指标（锁定 `test` 36 题） | CrossComply 问答 Agent | 同底座裸模型 (`direct`) | 差值 |
| --- | ---: | ---: | ---: |
| **总体通过率（`overall_pass`）** | **97.2% (35/36)** | 86.1% (31/36) | **+11.1 pp** |
| **不含直接答案子集通过率（31 题）** | **96.8% (30/31)** | 87.1% (27/31) | **+9.7 pp** |
| **法律准确性完全无瑕（`legal_correctness = pass`）** | **77.8% (28/36)** | 13.9% (5/36) | **+63.9 pp** |
| **法条/条号小错率（`minor_issue`）** | **19.4% (7/36)** | 72.2% (26/36) | **-52.8 pp** |
| **危险错误率（`wrong_definite`）** | **0.0% (0/36)** | 2.8% (1/36) | **-2.8 pp** |
| **确定性法源引用门禁通过率** | **97.2% (35/36)** | —（无结构化引用） | — |

- **多轮证据召回（36/36）**：必引条款证据召回 **90.5%**（不含直接答案子集 **88.9%**）、回答引用率 **85.1%**、必检来源召回 **90.0%**。
- **工程延迟与成本（官方默认 `deepseek-flash`）**：单查询检索 **P50 1.37 s / P95 1.51 s**、批量查询（2.3 条/批）**P50 2.44 s / P95 3.40 s**；端到端多轮问答中位数 **1 次检索 / 4 次精读**，**P50 54.1 s / P95 76.0 s**，无缓存成本上限均值 **¥0.29 / 次**（叠加官方 Prefix Cache 约 **¥0.10–0.18 / 次**）。

---

## 快速启动

**前置依赖**：`Python 3.11+` · `Node.js 22.12+` · `Docker & Docker Compose`

```bash
# 1. 安装后端依赖（如需本地运行 CLI 或测试）
pip install -e ".[dev,service]"

# 2. 配置环境变量（填入数据库、SeaweedFS、大模型 API 与 Embedding/Rerank 地址）
cp .env.example .env

# 3. 启动全部容器服务，执行数据库迁移并初始化管理员
docker compose up -d --build
python -m alembic upgrade head
python -m law_agent.review.bootstrap_admin
```

<details>
<summary>本地三进程调试与运行自动化测试</summary>

```bash
# 宿主机分别启动 API、审查 Worker 与知识库 Worker
uvicorn law_agent.review.api:app --reload
python -m law_agent.review.worker
python -m law_agent.kb.enrichment_worker

# 启动前端工作台
cd frontend && npm install && npm run dev

# 运行后端（1,149+ 单测）与前端全量检查
pytest
cd frontend && npm test && npm run build
```

</details>

---

## 技术栈

| 层级 | 核心组件 |
| --- | --- |
| **后端与运行时** | Python 3.11+, FastAPI, Pydantic v2, SSE Streaming, WASI Python Sandbox, OpenAI-Compatible Client |
| **混合检索与模型** | Elasticsearch (BM25), PostgreSQL + `pgvector` (`bge-m3`), Cross-Encoder (`bge-reranker-v2-m3`), Docling OCR |
| **存储与企业集成** | PostgreSQL, SeaweedFS (S3), Alembic, 飞书开放平台审批与提醒 |
| **前端工作台** | React, TypeScript, Vite |

<details>
<summary>仓库目录速览</summary>

| 路径 | 职责说明 |
| --- | --- |
| `law_agent/review/` | 案件审查与合规问答运行时、Worker、API 路由（`http/`）、引用门禁、语义校验、同主体累计、变更影响、飞书审批与 PDF 报告 |
| `law_agent/review/retrieval/` | 查询级 Overlay `scope`、ES + pgvector 混合检索、RRF 融合、时效过滤、校准重排与新旧法关联 |
| `law_agent/data/` | 离线六阶段数据治理流水线、多格式解析（DOCX / HTML / PDF / Docling OCR）、结构感知分块与引用资格策略 |
| `law_agent/kb/` | 在线知识库入库、模型优先网页去噪与新旧法抽取、法规变更检测与补库核验 Worker |
| `law_agent/llm/` | 统一 SSE 流式 LLM 客户端、`json_repair` 兜底、重试预算与 Token / TTFT 计费统计 |
| `law_agent/evals/` | 78 题防泄漏评测场景库、离线查询回放、多轮证据召回与跨模型双盲评测框架 |
| `frontend/` | React + Vite 前端工作台（含公开演示站数据 `src/demo/`） |
| `alembic/` & `docker/` & `scripts/` | 数据库迁移、容器镜像与 Nginx 配置、语料打包迁移与运维脚本 |
| `tests/` | 1,149+ 自动化单元测试与集成测试 |

</details>

---

## 许可

本项目基于 [MIT](LICENSE) 协议开源。
