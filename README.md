# CrossComply — Cross-Border Data Compliance Agent

[![CI](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml)

CrossComply 是一个面向企业数据出境/跨境数据合规审查场景的 Agentic RAG 项目，主线是“材料输入 -> 审查事实抽取 -> 混合检索 -> 证据自检 -> 受控二次召回 -> 结构化审查结果与引用”。复杂审查可使用确定性 Multi-Agent 模式，由 LLM Case Analyst、按议题 Evidence Researchers、Compliance Reviewer 和条件式 Evidence Critic 协作；Critic 最多触发一次定向补检索和一次证据约束的局部 patch 修订，检索不可满足的法规要求会降级为 evidence gap，而不是交给模型猜测。

## Product preview

[在线体验 CrossComply 前端演示](https://crosscomply-agent.vercel.app)

![CrossComply 审查报告与引用依据](docs/images/crosscomply-review-report.png)

公开站内置一份由真实 service 检索生成的审查报告：结论中的关键法律依据与合规义务直接关联右侧法源证据，完整工作流默认收起。公开站不提供共享审查后端；需要运行新的审查时，请按下文部署完整服务。

项目文档只保留长期维护入口：本文件记录日常开发、运行、启动和评测流程；[系统架构](docs/architecture.md) 解释 Supervisor、Agent 与检索模块；[当前手工标注评测对比](docs/evaluation/optimized-current-comparison.md) 记录 LLM、rerank 与 Multi-Agent 的质量/成本权衡；[三条真实 Trace](docs/evaluation/typical-traces.md) 展示直接成功、二次召回和 Critic 修订；[docs/SERVICE_STACK.md](docs/SERVICE_STACK.md) 记录 Elasticsearch + pgvector 部署；[docs/CONTEXT.md](docs/CONTEXT.md) 记录领域语言。

## Full evaluation result

| Metric | LLM | LLM + rerank | Bounded Multi-Agent |
|---|---:|---:|---:|
| Recall@5 | 0.8432 | 0.8586 | **0.8607** |
| Must-have Recall@5 | 0.8706 | **0.8925** | 0.8794 |
| Optional coverage@5 | 0.6964 | 0.6607 | **0.7857** |
| MRR@10 | 0.8553 | **0.8816** | 0.8575 |
| Candidate Recall@50 | 0.9200 | 0.9200 | **0.9507** |
| Bad cases (`Recall@5 < 0.5`) | 4 | 6 | **2** |
| Abstention accuracy | 1.0000 | 1.0000 | 1.0000 |
| Mean total latency | **10.37 s** | 10.40 s | 19.46 s |
| Total LLM calls | **84** | **84** | 215 |
| Duplicate sources@10 | **0.0000** | **0.0000** | 0.2073 |

三组均使用相同 82 case、真实 service 检索、DeepSeek Flash 和手工复核的核心/辅助法源标签。Must-have 覆盖 76 个有核心法源的场景，Optional 仅覆盖 28 个真正需要指南、模板、Q&A 或国标的场景。rerank 在低调用成本下提升核心法源与排序质量，但存在个别案例回退和 embedding 服务超时风险；Multi-Agent 主要换取更广的辅助来源覆盖。详细口径见[当前评测对比](docs/evaluation/optimized-current-comparison.md)。

## 开发命令

```powershell
python -m law_agent.data --help
pytest
```

## 一键启动与评测流程

这段是给新接手项目的人照着跑的最短路径，目标是启动 ES + pgvector、完成语料索引、检查服务健康、启动 API 和前端，并跑一轮评测。

### 1. 准备环境

```powershell
Copy-Item .env.example .env
pip install -e ".[service]"
```

编辑 `.env`，至少填入：

- `OPENAI_COMPATIBLE_API_KEY`
- `EMBEDDING_API_KEY`
- 如不使用默认本地服务，再调整 `ES_URL`、`PG_DSN`、`ES_INDEX`、`PG_TABLE`

### 2. 启动 ES + pgvector

```powershell
docker compose up -d --build
docker compose ps
```

等 Elasticsearch 和 Postgres 都变成 healthy 后继续。

### 3. 索引语料并检查服务

```powershell
python -m law_agent.review index-service --execute
python -m law_agent.review service-doctor
```

`service-doctor` 应看到：

- `elasticsearch: True`
- `postgres: True`
- `elasticsearch_docs` 非 0
- `pgvector_rows` 非 0

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
| `frontend/` | React + Vite 单用户合规研究工作台 |
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
docker exec lawagent-pg psql -U lawagent -c "DROP TABLE IF EXISTS lawagent_chunks;"
python -c "from law_agent.config import load_service_config; from law_agent.review.retrieval.service_backends import create_elasticsearch_client; c=load_service_config(); es=create_elasticsearch_client(c); es.indices.delete(index=c.elasticsearch.index_name, ignore_unavailable=True); es.close()"

# 3. 重新索引
python -m law_agent.review index-service --execute
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

详细架构和运维说明见 [docs/SERVICE_STACK.md](docs/SERVICE_STACK.md)。

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

公开作品演示可在构建时设置 `VITE_PUBLIC_DEMO=true`，前端将默认打开仓库内置案例，并禁用对共享后端的实时审查请求：

```powershell
$env:VITE_PUBLIC_DEMO = "true"
npm run build
```

若要部署可执行实时审查的个人实例，请先自行部署本仓库的 FastAPI、Elasticsearch 与 pgvector 服务，再让前端通过同域反向代理访问 `/api`；本地开发可直接使用上述 Vite 代理。也可以在构建前端时设置 `VITE_API_BASE_URL` 指向自行部署的 API，并相应配置后端允许该前端域名跨域访问。

### API 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/review` | 提交审查请求（表单或文件上传） |
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
