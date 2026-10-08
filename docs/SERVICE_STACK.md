# CrossComply 服务栈

当前部署事实。架构与产品设计见 [architecture.md](./architecture.md)，数据治理见 [data-governance-design.md](./data-governance-design.md)。

## 服务拓扑（docker compose）

| 服务 | 镜像 / 入口 | 职责 |
| --- | --- | --- |
| `nginx` | nginx 1.27，宿主机 `8080`（`CROSSCOMPLY_HTTP_PORT` 可改） | 唯一对外入口，同域反代前端静态资源与 `/api` |
| `frontend` | `docker/frontend.Dockerfile` 构建的静态资源 | React 工作台，由 nginx 提供 |
| `api` | `docker/backend.Dockerfile`，uvicorn :8000（宿主机 8000 同步映射） | FastAPI（`law_agent.review.api:app`） |
| `worker` | 同后端镜像，`python -m law_agent.review.worker` | 独立审查 Worker，领取 ReviewTask 跑生产 Agent |
| `knowledge-worker` | 同后端镜像，`python -m law_agent.kb.enrichment_worker` | 后台补库核验，不领取审查任务 |
| `postgres` | `pgvector/pgvector:pg16`，仅绑 `127.0.0.1:5432` | 案件、任务、用户、整改、审批与审计数据；pgvector 向量索引 |
| `elasticsearch` | ES 8.13.0 + IK 插件（`docker/elasticsearch.Dockerfile` 烘焙），仅绑 `127.0.0.1:9200` | 关键词检索 |
| `minio` | MinIO，容器内网 :9000（不对宿主机发布） | 材料原件与 PDF 决策报告 |
| `minio-init` | 一次性任务 | 创建 bucket（默认 `crosscomply-materials`） |
| `migrate` | 一次性任务 | API/Worker 启动前执行 `alembic upgrade head` |

依赖顺序：`migrate` 成功完成、ES healthy、bucket 初始化完成后 `api`/`worker` 才启动。网络分 `backend` 与内网 `frontend`，只有 nginx 跨两个网络；数据服务不直接暴露公网。

数据卷：`pgdata`、`esdata`、`miniodata`。语料目录通过 `CROSSCOMPLY_CORPUS_DIR`（默认 `./data/corpus/legal_docs_20260702`）以 rw 挂载进三个后端服务。

## 必需配置

复制 `.env.example` 为 `.env`，至少填写：

- `POSTGRES_PASSWORD`、`MINIO_ROOT_PASSWORD`（compose 强制要求）
- `OPENAI_COMPATIBLE_API_KEY`（审查 Agent 与各 LLM 节点，OpenAI 兼容协议）
- `EMBEDDING_API_KEY`（默认 SiliconFlow BGE-M3，1024 维）
- 飞书审批集成：`CROSSCOMPLY_FEISHU_APP_ID`、`CROSSCOMPLY_FEISHU_APP_SECRET`、`CROSSCOMPLY_FEISHU_APPROVAL_CODE`、`CROSSCOMPLY_FEISHU_INITIATOR_OPEN_ID`、`CROSSCOMPLY_FEISHU_VERIFICATION_TOKEN`、`CROSSCOMPLY_FEISHU_ENCRYPT_KEY`，并把 `CROSSCOMPLY_PUBLIC_BASE_URL` 设为审批人可访问的工作台地址（默认 `http://127.0.0.1:8080`）
- 可选：`CROSSCOMPLY_FEISHU_RECHECK_OPEN_ID`（新法源待复核提醒收件人）、`EXA_API_KEY`（受控官方 Web 调查；不配置时该能力对 Agent 不可用，不影响审查）
- `RERANK_MODE` 默认 `off`，仅在评测证明收益时打开

## 启动与验证

```powershell
docker compose up -d --build
docker compose ps
```

ES 与 Postgres 均 healthy、migrate 与 minio-init 完成后，统一入口为 `http://127.0.0.1:8080`。

首次建库不预置任何账号，显式创建第一个管理员：

```powershell
$env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD = "请替换为至少 12 位的强密码"
docker compose run --rm -e CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD=$env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD api python -m law_agent.review.bootstrap_admin --username admin@example.com --display-name "系统管理员"
Remove-Item Env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD
```

索引语料并检查服务：

```powershell
docker compose exec -T api python -m law_agent.review index-service --execute
docker compose exec -T api python -m law_agent.review service-doctor
```

`service-doctor` 应看到 `elasticsearch: True`、`postgres: True`、`elasticsearch_docs` 与 `pgvector_rows` 非 0。健康检查：`GET /api/health`（含 LLM 配置状态、ES/PG 连通性、索引计数）。

## 日常运维事实

- **迁移**：更新服务前先跑迁移；compose 的 `migrate` 服务每次启动自动 `alembic upgrade head`，迁移文件在 `alembic/versions/`。
- **语料索引**：`index-service` 同时写 ES（关键词）与 pgvector（向量，HNSW，BGE-M3 1024 维）；切换 embedding 模型或维度后必须删旧索引/表再重建。
- **新增法源**：`python -m law_agent.kb ingest <file>`；新 chunk 先不可检索，校验一致后原子切换。
- **飞书审批表单**：审批定义中创建六个单行文本控件，控件 ID 依次为 `case_number`、`title`、`decision_summary`、`key_actions`、`case_url`、`task_id`；审批人在飞书内完成通过或拒绝，正式 PDF 在终态回写后生成。
- **IK 分词**：`ik_max_word` 索引、`ik_smart` 搜索；插件版本必须与 ES 8.13.0 匹配，运行时在 IK → smartcn → standard 间自动探测。
- **停止**：`docker compose stop` / `docker compose down` 保留数据；仅在明确要清空数据卷重建时使用 `docker compose down -v`。

## 本地开发（不起全量 compose）

```powershell
pip install -e ".[service]"
python -m law_agent.review serve --host 127.0.0.1 --port 8000
```

本地仍需可连通的 ES 与 Postgres（可用 compose 只起 `postgres`、`elasticsearch`）。前端在 `frontend/` 下 `npm install && npm run dev`，Vite 把 `/api` 代理到 `127.0.0.1:8000`（Node `^20.19.0` 或 `>=22.12.0`）。
