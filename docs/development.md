# 开发指南

本地开发时，PostgreSQL、Elasticsearch、MinIO 跑在 Docker 里，API、Worker 和前端直接在宿主机运行。全量容器部署见 [部署](deployment.md)，变量说明见 [配置参考](configuration.md)。

## 1. 准备环境

需要 Git、Python 3.11+、Node.js 22.12+、Docker（Linux 容器）。

```bash
git clone https://github.com/raydemo1/crosscomply-agent.git
cd crosscomply-agent
python -m pip install -e ".[dev,service]"
cd frontend && npm ci && cd ..
```

创建 `.env`：

- Windows：`pwsh -File scripts/setup-dev.ps1`。它只在 `.env` 不存在时从 `.env.example` 生成，并写入随机的数据库和 MinIO 密码。
- 其他系统：`cp .env.example .env`，把 `change-this-postgres-password`、`change-this-minio-password` 替换为同一组随机值（`PG_DSN` 中的密码必须和 `POSTGRES_PASSWORD` 一致）。

模型密钥可以先留空；真实检索需要 `EMBEDDING_API_KEY`，审查和问答需要 `OPENAI_COMPATIBLE_API_KEY`。

宿主机上的后端命令都从进程环境读取配置。`law_agent` 的 CLI 会自动加载 `.env`，但 `alembic` 不会，运行迁移前需要先导出 `.env`（例如 `dotenv -f .env run -- alembic upgrade head`，或在 PowerShell 中逐行设置）。

## 2. 启动依赖服务

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres elasticsearch minio minio-init
alembic upgrade head
```

`docker-compose.dev.yml` 只额外把 MinIO 的 9000/9001 端口绑定到本机；PostgreSQL（5432）和 ES（9200）在主 compose 文件中就只绑定 127.0.0.1。

如果拉取 MinIO 镜像失败（例如镜像仓库返回 401），在 `.env` 中把 `MINIO_IMAGE` / `MINIO_MC_IMAGE` 指向可访问的固定版本镜像，或从已有机器 `docker save` / `docker load` 搬运镜像后加 `--pull never` 启动。任何兼容 S3 的服务都可以替代 MinIO，只要 `CROSSCOMPLY_MINIO_*` 指向它。

## 3. 恢复语料与检索索引

法律语料不在 Git 中，通过带校验清单的压缩包在机器间传递：

```bash
# 核对压缩包的 SHA256（与同名 .sha256 文件比对）后，解压到仓库根目录
python scripts/transfer_corpus.py verify
python scripts/transfer_corpus.py restore --execute
```

`restore` 先校验全部文件与缓存向量，再把向量和来源版本写入 ES 与 pgvector，不调用任何模型。目标索引必须为空，有旧数据时会退出而不覆盖。目标机器的 Embedding 模型和维度必须与打包时一致。

重新打包（先停止会修改语料的补库任务）：

```bash
python scripts/transfer_corpus.py pack --output output/transfer/crosscomply-corpus-<日期>.zip
```

`index-service --execute` 会从 `chunks.jsonl` 重建索引并可能调用 Embedding API；恢复已有语料时用 `restore`。

## 4. 创建管理员

```bash
CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD='<至少12位强密码>' \
  python -m law_agent.review.bootstrap_admin --username admin@example.com --display-name '开发管理员'
```

`bootstrap_admin` 只在数据库中还没有任何用户时可用。之后的账号在工作台「用户管理」中创建。

## 5. 运行

三个终端分别运行：

```bash
python -m law_agent.review serve --host 127.0.0.1 --port 8000   # API，/docs 为 OpenAPI
python -m law_agent.review.worker                                # 审查与问答 Worker（会产生模型费用）
cd frontend && npm run dev                                       # http://127.0.0.1:5173
```

Vite 把 `/api` 代理到 `127.0.0.1:8000`。同时只运行一个 Worker，否则重试的任务可能被旧进程领取。日常开发不需要启动 `knowledge-worker`。

检查服务连通性和索引规模：

```bash
python -m law_agent.review service-doctor
```

`npm run dev -- --mode demo` 启动使用虚构数据的演示模式，不需要后端。

## 测试

```bash
pytest                       # 后端；不访问外部模型和服务
cd frontend && npm test      # node:test 运行 src/**/*.test.ts
cd frontend && npm run build # tsc 类型检查 + Vite 构建
```

- `tests/conftest.py` 把密码哈希换成最低成本的 Argon2 参数；生产参数单次约 60ms，而几乎每个 API 测试都要建用户、登录。
- 前端测试命名为 `*.test.ts` 即被自动发现。需要渲染 TSX 组件的测试用 `src/testing/ssrServer.ts` 创建 Vite SSR 服务（已关闭依赖扫描，否则每个文件会多等十几秒）。
- 测试按被测模块组织（如 `test_source_navigation.py`、`test_decision_report.py`），新测试加到对应文件，不为单个场景新建文件。
- 前端 `tsconfig` 开启了 `noUnusedLocals` / `noUnusedParameters`；后端用 `ruff check --select F` 检查未使用的导入和变量。

## 约定

- **数据库迁移**：在 `alembic/versions/` 新增文件，编号顺延（`00NN_<描述>.py`），并同步更新 `law_agent/review/db_schema.py` 中的 `EXPECTED_SCHEMA_REVISION`。服务启动时会校验数据库已迁移到该版本。
- **HTTP 路由**：按业务放在 `law_agent/review/http/` 对应模块，只做认证、校验、错误映射；领域逻辑放在 store 或领域模块。路由函数中有阻塞 I/O（数据库、文件解析、模型调用）时，要么写成普通 `def`，要么用 `run_in_threadpool` 包装，不能在 `async def` 中直接调用。
- **存储**：每个 store 都有内存实现供测试使用，新增方法时两边同时实现。
- **前端类型**：`frontend/src/types/api.ts` 与后端 Pydantic 模型手工对应，改接口时同时更新。
- **评测会花钱**：`eval`、`agent-eval` 会调用付费模型，日常开发不要跑全量评测，见 [评测](evaluation.md)。

## 常用 CLI

| 命令 | 作用 |
| --- | --- |
| `python -m law_agent.review serve` | 启动 API |
| `python -m law_agent.review index-service --execute` | 从 `chunks.jsonl` 重建 ES 与 pgvector 索引 |
| `python -m law_agent.review service-doctor` | 检查 ES / PG / 索引计数 |
| `python -m law_agent.review eval` / `agent-eval` | 检索 benchmark / 生产 Agent 评测 |
| `python -m law_agent.kb ingest <文件>` / `list` / `remove` | 知识库入库与管理 |
| `python -m law_agent.data pipeline run` | 离线数据治理流水线 |
