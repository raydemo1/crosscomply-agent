# 部署

生产形态是一套 Docker Compose。架构见 [架构](architecture.md)，变量见 [配置参考](configuration.md)。

## 服务拓扑

| 服务 | 来源 | 职责 | 端口 |
| --- | --- | --- | --- |
| `nginx` | `nginx:1.27-alpine` + `docker/nginx.conf` | 统一入口，同域反代前端与 `/api` | 宿主机 `8080`（`CROSSCOMPLY_HTTP_PORT`） |
| `frontend` | `docker/frontend.Dockerfile` | 构建好的静态工作台 | 仅内网 |
| `api` | `docker/backend.Dockerfile` | FastAPI（`law_agent.review.api:app`） | `127.0.0.1:8000`（仅本机调试） |
| `worker` | 同上，`review-worker` 阶段（含 WASI Python 沙箱） | 审查与问答任务 | — |
| `knowledge-worker` | 同上 | 补库核验、飞书待办提醒 | — |
| `postgres` | `pgvector/pgvector:pg16` | 业务数据与向量索引 | `127.0.0.1:5432` |
| `elasticsearch` | `docker/elasticsearch.Dockerfile`（8.13.0 + IK 分词） | 关键词检索 | `127.0.0.1:9200` |
| `minio` | `MINIO_IMAGE` | 材料原件与 PDF 报告 | 仅内网 |
| `minio-init` | `MINIO_MC_IMAGE`，一次性 | 创建 bucket | — |
| `migrate` | 后端镜像，一次性 | `alembic upgrade head` | — |

启动顺序：`migrate` 完成、ES healthy、bucket 创建完成后，`api` 与 `worker` 才启动。只有 nginx 同时在 `frontend`（内部网络）和 `backend` 两个网络中。

数据卷：`pgdata`、`esdata`、`miniodata`。语料目录通过 `CROSSCOMPLY_CORPUS_DIR`（默认 `./data/corpus/legal_docs_20260702`）读写挂载到三个后端服务。

## 首次部署

1. 准备 `.env`：复制 `.env.example`，至少填写 `POSTGRES_PASSWORD`、`MINIO_ROOT_PASSWORD`（Compose 强制要求）、`OPENAI_COMPATIBLE_API_KEY`、`EMBEDDING_API_KEY`。
2. 启动：

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

3. 创建第一个管理员（数据库中没有用户时才允许）：

   ```bash
   docker compose run --rm -e CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD='<至少12位强密码>' \
     api python -m law_agent.review.bootstrap_admin --username admin@example.com --display-name '系统管理员'
   ```

4. 建立检索索引并检查：

   ```bash
   docker compose exec -T api python -m law_agent.review index-service --execute
   docker compose exec -T api python -m law_agent.review service-doctor
   ```

   `service-doctor` 应显示 ES 与 PostgreSQL 均可连接，`elasticsearch_docs` 和 `pgvector_rows` 不为 0。已有语料包时可改用 `scripts/transfer_corpus.py restore`，见 [开发指南](development.md#3-恢复语料与检索索引)。

入口为 `http://<主机>:8080`。

## 持续审查升级

升级已有实例前先备份数据库，停止 API、审查 Worker 和知识库 Worker。使用新版本镜像运行迁移和主体键回填：

```bash
docker compose build migrate api
docker compose run --rm migrate alembic upgrade head
docker compose run --rm --no-deps api python -m law_agent.review backfill-entity-keys
docker compose run --rm --no-deps api python -m law_agent.review backfill-citation-index --dry-run
docker compose run --rm --no-deps api python -m law_agent.review backfill-citation-index
```

主体键和引用索引回填均可重复执行，不改写审查报告或审批。引用索引读取当前启用的语料；非默认目录用 `--chunks` 指定。完成后再启动新版本服务。知识库 Worker 每日提升已生效的新版本提示，也可用 `impact-scan --as-of YYYY-MM-DD` 运维触发。没有新增生产环境变量。

## 飞书集成（可选）

**审批**：配置 `CROSSCOMPLY_FEISHU_APP_ID`、`APP_SECRET`、`APPROVAL_CODE`、`INITIATOR_OPEN_ID`、`VERIFICATION_TOKEN`、`ENCRYPT_KEY`，并把 `CROSSCOMPLY_PUBLIC_BASE_URL` 设为审批人能访问的工作台地址。审批定义中按顺序创建六个单行文本控件，ID 为 `case_number`、`title`、`decision_summary`、`key_actions`、`case_url`、`task_id`。审批人在飞书完成通过或拒绝，事件验签后写回案件终态，正式 PDF 在此之后生成。

**待办提醒**：由 `knowledge-worker` 发送。`CROSSCOMPLY_FEISHU_WORK_RECIPIENTS` 是登录名到同一机器人应用下 `open_id` 的 JSON 映射，`CROSSCOMPLY_WORK_REVIEWER_USERNAME` 指定法务公共队列的收件人。未映射的用户仍可在站内处理待办。到期提醒按北京时间，从截止日前两天开始。`CROSSCOMPLY_FEISHU_RECHECK_OPEN_ID` 接收新法源待复核提醒。

## 运维

- **健康检查**：`GET /api/health` 只返回 `{"status": "ok" | "degraded"}`（ES、PG 可达且 LLM 已配置时为 ok）。管理员可通过 `GET /api/admin/diagnostics` 查看模型、服务和索引详情。
- **升级**：`docker compose up -d --build`。每次启动 `migrate` 都会先执行迁移。
- **新增法源**：工作台「知识库」或 `python -m law_agent.kb ingest <文件>`。新 chunk 先以不可检索状态写入，校验一致后原子切换。
- **更换 Embedding 模型或维度**：必须删除旧的 ES 索引和 pgvector 表后重新 `index-service --execute`。
- **IK 分词**：索引用 `ik_max_word`，查询用 `ik_smart`；插件版本必须与 ES 8.13.0 一致。运行时按 IK → smartcn → standard 自动选择可用分析器。
- **停止**：`docker compose stop` 或 `down` 保留数据；`down -v` 会删除所有数据卷，仅在确定要清空时使用。
- **备份**：案件数据在 PostgreSQL 与 MinIO 两处，需同时备份；语料用 `transfer_corpus.py pack` 打包。

## 安全注意

- `api`、`postgres`、`elasticsearch` 的端口只绑定 127.0.0.1，外部访问一律经过 nginx。
- 会话 Cookie 在 HTTPS（含反代传入的 `X-Forwarded-Proto: https`）下自动加 Secure；也可用 `CROSSCOMPLY_COOKIE_SECURE` 显式指定。
- 登录失败 5 次后按用户名 + 来源 IP 指数退避锁定（进程内计数，多副本部署时各自计数）。

## 公开演示站

`frontend/vercel.json` 用 `npm run build:demo` 构建一个纯前端演示站。示例案件、材料与回答全部预置在 `frontend/src/demo/`，不访问后端或模型；未知路径和写操作直接拒绝，CSP 设置 `connect-src 'none'`。`npm run build` 与 Docker 构建不受影响。
