# CrossComply — Cross-Border Data Compliance Agent

[![CI](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml)

CrossComply 是一个面向企业数据出境/跨境数据合规审查场景的 Agentic RAG 项目，主线是“材料输入 -> 审查事实抽取 -> 混合检索 -> 证据自检 -> 受控二次召回 -> 结构化审查结果与引用”。复杂审查可使用确定性 Multi-Agent 模式：Case Analyst 依次提取事实、拆分议题并按议题并行生成查询；Evidence Researchers 按议题执行检索，Evidence Gate 确定性形成证据 dossier；Compliance Reviewer 复用受控报告生成链路，结合议题计划和 dossier 写出结果；条件式 Evidence Critic 只作 `accept`、`research_required` 或 `revision_required` 路由，最多复用一次 Researcher 补证和一次 Reviewer 修订。检索不可满足的法规要求会披露为 evidence gap，而不是交给模型猜测。

在完整部署中，这条审查链路可用作企业数据出境上线前的合规闸门：确定性规则负责硬条件计算，LLM/Multi-Agent 继续负责证据化深审，最终决定由飞书审批回写。

## Product preview

[在线体验 CrossComply 前端演示](https://crosscomply-agent.vercel.app)

公开站内置一份由真实 service 检索生成的审查报告：结论中的关键法律依据与合规义务直接关联右侧法源证据，完整工作流默认收起。公开站不提供共享审查后端；需要运行新的审查时，请按下文部署完整服务。

## Full evaluation result

| Metric | LLM | LLM + rerank | Bounded Multi-Agent |
|---|---:|---:|---:|
| Recall@5 | 87.06% | 87.72% | **90.57%** |
| Must-have Recall@5 | 89.04% | 89.91% | **92.54%** |
| Optional coverage@5 | 82.14% | 75.00% | **85.71%** |

三组均使用相同的 82 个场景、冻结事实与查询输入、真实 service 检索、DeepSeek-V4-Flash，以及人工复核的核心/辅助法源标签。Must-have 覆盖 76 个含核心法源的场景，Optional 覆盖 28 个需要指南、模板、Q&A 或国标补充的场景。相较 LLM 基线，有界 Multi-Agent 的 Recall@5、Must-have Recall@5 和 Optional coverage@5 分别提升 3.51pp、3.50pp 和 3.57pp。

> 上表对应已冻结的 82 个评测场景与现有产物。企业流程功能开发默认不触发高成本模型评测；仅在需要更新对外指标时显式重跑。

## 开发命令

```powershell
python -m law_agent.data --help
pytest
```

## 一键启动与评测流程

这段是给新接手项目的人照着跑的最短路径，目标是启动 ES + pgvector、完成语料索引、检查服务健康、启动 API 和前端，并跑一轮评测。Docker 编排同时包含独立 worker、MinIO、反向代理和 Alembic 基线初始化。

### 1. 准备环境

```powershell
Copy-Item .env.example .env
pip install -e ".[service]"
```

编辑 `.env`，至少填入：

- `POSTGRES_PASSWORD`
- `MINIO_ROOT_PASSWORD`
- `OPENAI_COMPATIBLE_API_KEY`
- `EMBEDDING_API_KEY`
- 飞书集成时填入 `CROSSCOMPLY_FEISHU_APP_ID`、`CROSSCOMPLY_FEISHU_APP_SECRET`、`CROSSCOMPLY_FEISHU_APPROVAL_CODE`、`CROSSCOMPLY_FEISHU_INITIATOR_OPEN_ID`、`CROSSCOMPLY_FEISHU_VERIFICATION_TOKEN`、`CROSSCOMPLY_FEISHU_ENCRYPT_KEY`，并将 `CROSSCOMPLY_PUBLIC_BASE_URL` 设为审批人可访问的工作台地址
- 如不使用默认本地服务，再调整 `ES_URL`、`PG_DSN`、`ES_INDEX`、`PG_TABLE`

### 2. 启动服务栈

```powershell
docker compose up -d --build
docker compose ps
```

等 Elasticsearch 和 Postgres 都变成 healthy 后继续。

统一入口为 `http://127.0.0.1:8080`，数据服务不暴露到宿主机公网端口。

**首次部署账号：** 系统不创建预置申请人、审核人或管理员账号。新库首次启动后，显式执行：

```powershell
$env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD = "请替换为至少 12 位的强密码"
docker compose run --rm -e CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD=$env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD api python -m law_agent.review.bootstrap_admin --username admin@example.com --display-name "系统管理员"
Remove-Item Env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD
```

后续账号由管理员在系统内创建、停用、重置密码和分配角色。

**飞书审批表单：** 审批定义中创建六个单行文本控件，并将控件 ID 依次设为 `case_number`、`title`、`decision_summary`、`key_actions`、`case_url`、`task_id`。审批人直接在飞书查看风险、候选路径和关键整改项并完成通过或拒绝；只有需要核验材料原文、法源与完整证据链时才打开 `case_url`。正式 PDF 在飞书终态回写后生成。

**完整案例：** [`examples/hero_case/cross_border_saas/`](examples/hero_case/cross_border_saas/README.md) 提供一套脱敏的境外 CRM/AI SaaS 采购材料、人工确认事实、飞书演示事件和报告校验脚本。

### 3. 索引语料并检查服务

```powershell
docker compose exec -T api python -m law_agent.review index-service --execute
docker compose exec -T api python -m law_agent.review service-doctor
```

`service-doctor` 应看到：

- `elasticsearch: True`
- `postgres: True`
- `elasticsearch_docs` 非 0
- `pgvector_rows` 非 0

### 后续导入或更正知识库资料

日常不需要区分“新增”还是“更新”。将文件交给同一个入口即可：

```powershell
python -m law_agent.kb ingest .\新资料.pdf
```

程序先解析、清洗并按规范化正文查重：同一内容即使改了文件名也默认跳过；若正文变化且标题匹配已有来源，会让用户确认更新或作为独立来源。更新时，新 Chunk 先以不可检索状态写入 ES 和 pgvector，校验一致后才切换检索并删除旧 Chunk；未变化 Chunk 会复用 Embedding 缓存。批处理可额外传入 `--non-interactive --metadata source.json`。

### 4. 启动 API

```powershell
python -m law_agent.review serve --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health | ConvertTo-Json -Depth 6
```

返回内容会包含 LLM 配置状态、ES/PG 连通性和语料索引计数。

### 5. 启动前端

```powershell
Set-Location frontend
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。

### 6. 运行 golden-set 评测

快速 smoke：

```powershell
python -m law_agent.review eval --suite quick --review-mode llm --max-workers 8 --output data/review_runs/eval_quick_service.json --report data/review_runs/eval_quick_service.md
```

完整验证：

```powershell
python -m law_agent.review eval --suite full --review-mode multi_agent --max-workers 8 --output data/review_runs/eval_full_service.json --report data/review_runs/eval_full_service.md
```

评测汇总会包含检索质量指标，以及 `mean_total_latency_ms`、`mean_retrieval_latency_ms`、`total_llm_calls`、`total_retries`。

### 7. 停止服务

```powershell
docker compose stop
```

只有明确要删除服务数据并重建索引时，才使用 `docker compose down -v`。

## 项目结构

| 路径 | 用途 |
|---|---|
| `law_agent/data/` | manifest、fetch、normalize、clean、enrich、chunk、数据 evalset 流水线 |
| `law_agent/review/` | 材料驱动审查、混合检索、证据自检、评测和 FastAPI |
| `frontend/` | React + Vite 跨境数据合规案件工作台 |
| `data/corpus/legal_docs_20260702/` | 当前 review 语料包，本地生成数据，默认被 git 忽略 |
| `data/models/docling/` | Docling/RapidOCR 本地模型缓存，默认被 git 忽略 |
| `data/review_runs/` | 本地 review case、trace、result 输出，默认被 git 忽略 |

## 模型配置

语义增强阶段强制使用 OpenAI-compatible API，不提供 rule-based fallback。

复制 `.env.example` 为 `.env`，填入 DeepSeek 或其他 OpenAI-compatible provider：

```powershell
Copy-Item .env.example .env
```

`.env.example` 已预填 DeepSeek 官方 OpenAI-compatible 配置：

```text
OPENAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
OPENAI_COMPATIBLE_API_KEY=sk-your-deepseek-api-key
OPENAI_COMPATIBLE_MODEL=deepseek-v4-flash
OPENAI_COMPATIBLE_BETA_BASE_URL=https://api.deepseek.com/beta
OPENAI_COMPATIBLE_STRUCTURED_OUTPUT=strict_tool
OPENAI_COMPATIBLE_REASONING_EFFORT=none
LAWAGENT_LLM_MAX_RETRIES=3
LAWAGENT_LLM_FACT_MODEL=deepseek-v4-flash
LAWAGENT_LLM_QUERY_MODEL=deepseek-v4-flash
LAWAGENT_LLM_EVIDENCE_MODEL=deepseek-v4-flash
LAWAGENT_LLM_RESULT_MODEL=deepseek-v4-flash
```

配置后验证：

```powershell
python -m law_agent.data config check
```

## 文件流水线

```powershell
python -m law_agent.data manifest build --topic data_compliance --from-flk --limit 5
python -m law_agent.data manifest validate data/manifests/source_manifest.csv
python -m law_agent.data fetch
python -m law_agent.data normalize
python -m law_agent.data clean run
python -m law_agent.data enrich
python -m law_agent.data chunk
python -m law_agent.data evalset build
python -m law_agent.data report governance
```

或在配置好 manifest 和模型后运行：

```powershell
python -m law_agent.data pipeline run
```

FLK 采集链路使用国家法律法规数据库官方接口：

- `POST /law-search/search/list`：按主题生成 source manifest。
- `GET /law-search/download/pc?format=docx&bbbs=...`：解析公开签名下载 URL。
- DOCX 正文解析使用 Python 标准库 `zipfile` 和 XML parser，不引入额外依赖。

## Service Stack（Elasticsearch + pgvector）

`data/corpus/legal_docs_20260702/chunks.jsonl` 可索引到 Elasticsearch + pgvector，实现真实混合检索（关键词 + 向量 RRF 融合）。

### 前置条件

- Docker Desktop 已安装并运行（WSL2 后端）
- Python 3.11+

### 1. 安装依赖

```powershell
# 基础依赖
pip install -e .

# service 可选依赖（Elasticsearch + pgvector 客户端）
pip install -e ".[service]"
```

### 2. 配置环境变量

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，填入 LLM 和 Embedding 配置：

```text
# LLM（DeepSeek 或其他 OpenAI 兼容服务）
OPENAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
OPENAI_COMPATIBLE_API_KEY=sk-your-deepseek-api-key
OPENAI_COMPATIBLE_MODEL=deepseek-v4-flash
OPENAI_COMPATIBLE_BETA_BASE_URL=https://api.deepseek.com/beta
OPENAI_COMPATIBLE_STRUCTURED_OUTPUT=strict_tool
OPENAI_COMPATIBLE_REASONING_EFFORT=none
LAWAGENT_LLM_MAX_RETRIES=3
LAWAGENT_LLM_FACT_MODEL=deepseek-v4-flash
LAWAGENT_LLM_QUERY_MODEL=deepseek-v4-flash
LAWAGENT_LLM_EVIDENCE_MODEL=deepseek-v4-flash
LAWAGENT_LLM_RESULT_MODEL=deepseek-v4-flash

# Embedding（硅基流动 SiliconCloud，OpenAI 兼容）
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=sk-your-siliconcloud-api-key
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIM=1024
EMBEDDING_TIMEOUT_SECONDS=60

# Elasticsearch
ES_URL=http://localhost:9200
ES_INDEX=lawagent_chunks
ES_INDEX_NAME=lawagent_chunks

# PostgreSQL + pgvector
PG_DSN=postgresql://lawagent:lawagent@localhost:5432/lawagent
```

验证配置：

```powershell
python -m law_agent.data config check
```

### 3. 启动 ES + pgvector

```powershell
# 首次启动（构建 ES 镜像，预装 IK 中文分词插件）
docker compose up -d --build

# 后续启动（镜像已构建，直接启动）
docker compose up -d

# 查看状态，等两个服务都显示 healthy
docker compose ps
```

服务端口：

| 服务 | 地址 | 用途 |
|---|---|---|
| Elasticsearch | `http://localhost:9200` | 关键词检索（IK 中文分词） |
| PostgreSQL + pgvector | `localhost:5432` | 向量检索（BGE-M3 1024 维） |

数据持久化到 Docker 命名卷 `esdata`、`pgdata`，`docker compose down` 不会丢数据。

```powershell
# 停止服务（保留数据）
docker compose down

# 彻底清除数据（重建索引前需要）
docker compose down -v
```

### 4. 检查服务连通性

```powershell
python -m law_agent.review service-doctor
```

该命令会检查 ES 版本、PG 连接、Embedding provider 三个组件是否就绪。

### 5. 索引语料

确保 `data/corpus/legal_docs_20260702/chunks.jsonl` 存在后索引：

```powershell
python -m law_agent.review index-service --execute
```

该命令会将 chunks 写入 ES（关键词索引）和 pgvector（向量索引），使用 BGE-M3 生成 1024 维 embedding。

### 6. 检索

先创建 review case：

```powershell
python -m law_agent.review run `
  --question "这个场景是否需要数据出境安全评估？" `
  --material-text "我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。" `
  --output-dir data/review_runs
```

然后用 service 模式检索：

```powershell
# 从 review_cases.jsonl 获取 case_id
$caseId = (Get-Content data/review_runs/review_cases.jsonl | ConvertFrom-Json)[0].review_case_id

python -m law_agent.review retrieve `
  --case-id $caseId `
  --output-dir data/review_runs `
  --top-k 5
```

检索会同时查询 ES（关键词命中）和 pgvector（向量近邻），通过 RRF 融合排序后返回最终证据；服务不可用时会明确失败，不提供本地模拟回退。

### 切换 Embedding 模型

切换模型后需重建 pgvector 表（维度变化时必须）：

```powershell
# 1. 更新 .env 中的 EMBEDDING_MODEL / EMBEDDING_DIM

# 2. 删除旧索引
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP TABLE IF EXISTS lawagent_chunks;"'
docker compose exec -T api python -c "from law_agent.config import load_service_config; from law_agent.review.retrieval.service_backends import create_elasticsearch_client; c=load_service_config(); es=create_elasticsearch_client(c); es.indices.delete(index=c.elasticsearch.index_name, ignore_unavailable=True); es.close()"

# 3. 重新索引
docker compose exec -T api python -m law_agent.review index-service --execute
```

### 本地 Embedding（可选）

不想调用云端 API 时，可使用本地 sentence-transformers：

```powershell
pip install -e ".[local-embeddings]"
```

`.env` 改为：

```text
EMBEDDING_PROVIDER=sentence_transformers
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
EMBEDDING_DIM=1024
```

## 前端启动

前端基于 React 19 + Vite 8 + TypeScript，通过 Vite dev server 代理 `/api` 请求到后端 FastAPI。Vite 8 需要 Node `^20.19.0` 或 `>=22.12.0`。

### 1. 启动后端 API

```powershell
# 确保 data/corpus/legal_docs_20260702/chunks.jsonl 已准备并完成 service 索引
# 确保 .env 已配置 LLM API key

# 启动 FastAPI（端口 8000），前端只使用真实 service 检索
pip install uvicorn
python -m law_agent.review serve --host 0.0.0.0 --port 8000
```

后端启动后可访问：
- API 文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/api/health`

### 2. 启动前端

```powershell
cd frontend

# 安装依赖（首次）
npm install

# 启动 Vite dev server（端口 5173）
npm run dev
```

浏览器打开 `http://localhost:5173` 即可使用。

Vite dev server 会自动将 `/api/*` 请求代理到 `http://127.0.0.1:8000`（配置在 `frontend/vite.config.ts`），无需额外设置 CORS。

### 3. 生产构建

```powershell
cd frontend
npm run build
# 产物输出到 frontend/dist/
```

前端是服务端案件工作台，首次进入需要使用服务端账号登录。新库通过 `python -m law_agent.review.bootstrap_admin` 显式创建第一个管理员；后续账号、案件、材料快照、审查任务、整改动作和审计时间线均持久化在 PostgreSQL，原件与决策报告保存在 MinIO。

若要部署可执行实时审查的实例，请先自行部署本仓库的 FastAPI、Elasticsearch 与 pgvector 服务，再让前端通过同域反向代理访问 `/api`；本地开发可直接使用上述 Vite 代理。也可以在构建前端时设置 `VITE_API_BASE_URL` 指向自行部署的 API，并相应配置后端允许该前端域名跨域访问。

### API 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/auth/login` | 登录并创建服务端会话 |
| POST | `/api/auth/logout` | 注销当前会话 |
| GET | `/api/auth/me` | 获取当前用户与角色 |
| POST | `/api/cases` | 创建案件并保存采集事实 |
| GET | `/api/cases` | 获取当前用户可见的案件队列 |
| GET | `/api/cases/{case_id}` | 获取案件、结果、动作和时间线 |
| POST | `/api/cases/{case_id}/materials` | 上传并版本化保存材料原件 |
| POST | `/api/cases/{case_id}/material-snapshots` | 冻结材料快照并执行全国主路径判定 |
| POST | `/api/cases/{case_id}/run` | 创建异步证据化审查任务 |
| GET | `/api/tasks/{task_id}` | 查询审查任务、失败节点和重试记录 |
| PATCH | `/api/cases/{case_id}` | 更新案件材料与事实 |
| POST | `/api/cases/{case_id}/status` | 提交或退回补充案件 |
| POST | `/api/cases/{case_id}/feishu-approval` | 发起飞书审批 |
| POST | `/api/integrations/feishu/approval-events` | 验签并幂等回写审批终态 |
| POST | `/api/cases/{case_id}/reports` | 生成带 SHA-256 的 PDF 决策报告 |
| POST | `/api/cases/{case_id}/feedback` | 保存人工反馈与引用判定 |
| GET | `/api/cases/{case_id}/events` | 获取审计事件 |
| GET | `/api/dashboard/summary` | 获取案件状态与风险摘要 |
| POST | `/api/eval/run` | 触发评测运行 |
| GET | `/api/eval/latest` | 获取最近评测结果 |
| GET | `/api/health` | 健康检查 |

## 文档解析器

默认 `auto` 解析策略保持第一阶段轻量：FLK 法规 DOCX 继续使用标准库解析，HTML/JSON/文本走内置规则；PDF 和图片类文档会转给 Docling。Docling 默认在 OCR 阶段使用本地 RapidOCR + ONNXRuntime；如果需要把 OCR 放到远程 PaddleOCR 服务，可接 KServe v2-compatible OCR API。用户上传扫描版 PDF、复杂版式 PDF 时，也可以显式切到 MinerU。

```powershell
python -m law_agent.data normalize --parser auto
python -m law_agent.data normalize --parser docling
python -m law_agent.data normalize --parser mineru --parser-output-dir data/parser/mineru
```

Docling 和 MinerU 是可选重依赖，按需要安装：

```powershell
pip install -e ".[docling]"
pip install -e ".[mineru]"
# 或一次安装全部解析器
pip install -e ".[parsers]"
```

如果本地 `data/models/docling` 目录缺 RapidOCR 模型，流水线会让 Docling 回到默认模型缓存，避免卡在残缺目录上。要强制使用某个完整模型目录，可以设置：

```powershell
$env:LAWAGENT_DOCLING_ARTIFACTS_PATH="data/models/docling"
```

远程 OCR API 需要兼容 Docling 的 KServe v2 OCR 输入输出：输入包含 `image` 和 `lang_type`，输出包含 `boxes`、`txts`、`scores`。配置示例：

```powershell
$env:LAWAGENT_DOCLING_OCR_ENGINE="kserve_v2_ocr"
$env:LAWAGENT_DOCLING_OCR_API_URL="http://127.0.0.1:8000"
$env:LAWAGENT_DOCLING_OCR_MODEL_NAME="ocr"
$env:LAWAGENT_DOCLING_OCR_TRANSPORT="http"
python -m law_agent.data normalize --parser docling
```

取舍原则：

1. 纯文本法规、FLK DOCX：使用内置轻量 parser，速度快、依赖少。
2. 普通 PDF、Word、表格和版面结构：优先 Docling，便于导出 Markdown 并保留结构。
3. 扫描版/复杂 PDF：优先试 Docling；若版式结构仍不理想，再使用 MinerU pipeline，产出 Markdown 后进入清洗、分块和检索链路。
4. 不在当前项目内直接加载 PaddleOCR 本地模型；如果要用 PaddleOCR，优先把它封装成远程 KServe v2-compatible OCR 服务，让 Docling 在 OCR 阶段调用。

实现参考了 `ZongziForu/cn-law-hub` 对 FLK API 的公开整理，但当前仓库保留自己的数据治理 schema、清洗、语义增强、chunk 和 evalset 流水线。

## 当前范围

当前实现已包含 JSONL 数据治理流水线、Elasticsearch + pgvector 真实混合检索、材料驱动审查 API、Review eval full/quick 评测集和前端工作台。
