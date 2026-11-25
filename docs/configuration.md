# 配置参考

后端从进程环境读取配置；`law_agent` 的命令启动时会把仓库根目录的 `.env` 补充进环境（已存在的环境变量优先）。Compose 通过 `env_file` 加载同一个 `.env`，并覆盖服务间地址（ES、PG、对象存储的容器内地址）。模板见 [`.env.example`](../.env.example)。

代理变量（`HTTP_PROXY` 等）不从 `.env` 读取；若继承的代理端口无人监听，启动时会自动移除，避免所有外部请求打到失效代理。

## 模型（OpenAI 兼容）

在仓库根目录，把聊天端点写到 Git 忽略的 `model-profiles/.env.<名字>`。例如 `model-profiles/.env.infistar`：

```dotenv
OPENAI_COMPATIBLE_BASE_URL=https://infistar.cc/v1
OPENAI_COMPATIBLE_API_KEY=<填写该端点的密钥>
OPENAI_COMPATIBLE_MODEL=deepseek-v4.1-flash
OPENAI_COMPATIBLE_STRUCTURED_OUTPUT=json_object
LAWAGENT_AGENT_REASONING_EFFORT=none
LAWAGENT_SEMANTIC_REASONING_EFFORT=none
```

- 在项目的 `.env` 中设 `CROSSCOMPLY_LLM_PROFILE=infistar`，或在启动命令的进程环境里临时指定。设为 `default` 或省略选择器时，使用原来的 `.env` 和进程环境。
- 命名档独立提供聊天端点的 URL、密钥和模型。档案不存在或缺少必填项时直接报错。该模式下地址、模型、超时、输出模式、思考强度和模型使用边界都由档内设置及程序默认值决定。Embedding、Rerank、数据库和飞书仍从原来的配置读取。
- Compose 会把 `model-profiles/` 只读挂载到 API、审查 Worker 和知识库 Worker。切换容器见[部署说明](deployment.md#切换聊天端点)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CROSSCOMPLY_LLM_PROFILE` | `default` | 聊天端点配置档 |
| `OPENAI_COMPATIBLE_BASE_URL` | `https://api.deepseek.com` | |
| `OPENAI_COMPATIBLE_BETA_BASE_URL` | `<BASE_URL>/beta` | `strict_tool` 模式使用 |
| `OPENAI_COMPATIBLE_API_KEY` | — | 审查、问答、事实抽取必需 |
| `OPENAI_COMPATIBLE_MODEL` | `deepseek-flash` | |
| `OPENAI_COMPATIBLE_STRUCTURED_OUTPUT` | `strict_tool`（默认端点）、`json_object`（命名档） | `strict_tool` 或 `json_object` |
| `OPENAI_COMPATIBLE_REASONING_EFFORT` | `none` | 通用结构化节点；`strict_tool` 下必须为 none |
| `LAWAGENT_AGENT_REASONING_EFFORT` | `low` | 主 Agent 与案件问答 |
| `LAWAGENT_SEMANTIC_MODEL` | 继承任务模型 | 独立语义校验使用的模型 |
| `LAWAGENT_SEMANTIC_REASONING_EFFORT` | `low` | 独立语义校验 |
| `OPENAI_COMPATIBLE_TIMEOUT_SECONDS` | `60` | 单次请求超时 |
| `OPENAI_COMPATIBLE_READ_TIMEOUT_SECONDS` | `60` | 流式读取空闲超时 |
| `OPENAI_COMPATIBLE_TOTAL_TIMEOUT_SECONDS` | `120` | 流式请求总时限 |
| `OPENAI_COMPATIBLE_CONNECT_RETRY_SECONDS` | `45` | 连接失败时的重试时间预算 |
| `LAWAGENT_LLM_MAX_RETRIES` | — | 各节点格式校验失败的重试次数；可用 `LAWAGENT_LLM_{FACT,QUERY,EVIDENCE_CHECK,RESULT}_RETRIES` 按节点覆盖 |

思考强度可选 `none` / `low` / `high` / `max`。

## Embedding 与 Rerank

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `openai_compatible` | 或 `sentence_transformers`（需 `.[local-embeddings]`） |
| `EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | 模板中为 SiliconFlow |
| `EMBEDDING_API_KEY` | — | |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 模板中为 `BAAI/bge-m3` |
| `EMBEDDING_DIM` | `1536` | 模板中为 `1024`；必须与已建索引一致 |
| `EMBEDDING_TIMEOUT_SECONDS` | `60` | |
| `EMBEDDING_RETRY_SECONDS` | `30` | 429/5xx/网络错误的重试时间预算 |
| `RERANK_MODE` | `cross_encoder` | `cross_encoder`（融合后用 cross-encoder 重排每条查询的前 50 个候选）或 `off`；重排接口失败时退回融合排序 |
| `RERANK_BASE_URL` / `RERANK_API_KEY` / `RERANK_MODEL` | 继承 Embedding | 留空即继承；模型按 Embedding 配对，bge-m3 对应 bge-reranker-v2-m3 |
| `RERANK_TIMEOUT_SECONDS` | 继承 Embedding | |
| `RERANK_WINDOW` | `50` | |
| `RERANK_BLEND_WEIGHT` | `0.85` | 重排校准分在最终排序中的权重，其余为固定上界缩放后的融合分 |

## 检索服务

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ES_URL` | `http://localhost:9200` | |
| `ES_INDEX` | `lawagent_chunks` | 旧名 `ES_INDEX_NAME` 仍兼容 |
| `ES_API_KEY` | — | |
| `ES_VERIFY_CERTS` | `true` | |
| `PG_DSN` | `postgresql://lawagent:lawagent@localhost:5432/lawagent` | 业务库与向量库共用；也是 Alembic 的连接串 |
| `PG_TABLE` | `lawagent_chunks` | pgvector 表名 |
| `EXA_API_KEY` | — | 受控官方 Web 调查；不配置时该工具对 Agent 不可用 |

## 工作台

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CROSSCOMPLY_SESSION_TTL_HOURS` | `12` | 登录会话有效期 |
| `CROSSCOMPLY_COOKIE_SECURE` | 自动 | 显式指定会话 Cookie 的 Secure 标记 |
| `CROSSCOMPLY_MODEL_BOUNDARY` | `enterprise-approved-api`（默认端点）、`external-api`（命名档） | 记录在审查结果中的模型使用边界说明 |
| `CROSSCOMPLY_PUBLIC_BASE_URL` | — | 飞书消息中的工作台链接 |
| `CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD` | — | 仅 `bootstrap_admin` 使用 |
| `CROSSCOMPLY_WORKER_ID` | `主机名-PID` | |
| `CROSSCOMPLY_WORKER_POLL_SECONDS` | `2` | 无任务时的轮询间隔 |
| `CROSSCOMPLY_ENRICHMENT_WORKER_ID` | `主机名-PID` | knowledge-worker 标识 |
| `CROSSCOMPLY_REPORT_FONT` / `_BOLD` | 仓库 `docker/fonts/` | PDF 报告字体 |
| `LAWAGENT_WASI_PYTHON_ROOT` | — | `run_python` 沙箱的 CPython WASI 目录；Worker 镜像已内置 |

## 对象存储（SeaweedFS 或任意 S3 兼容服务）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CROSSCOMPLY_S3_ENDPOINT` | `objectstore:8333` | 宿主机开发用 `localhost:9000` |
| `CROSSCOMPLY_S3_ACCESS_KEY` / `SECRET_KEY` | — | 必须与存储服务账户一致 |
| `CROSSCOMPLY_S3_BUCKET` | `crosscomply-materials` | 不存在时由应用创建 |
| `CROSSCOMPLY_S3_SECURE` | `false` | |


## 飞书

`CROSSCOMPLY_FEISHU_APP_ID`、`APP_SECRET`、`APPROVAL_CODE`、`INITIATOR_OPEN_ID`、`VERIFICATION_TOKEN`、`ENCRYPT_KEY`（审批）；`RECHECK_OPEN_ID`（法源复核提醒）；`WORK_RECIPIENTS`（登录名 → open_id 的 JSON）与 `CROSSCOMPLY_WORK_REVIEWER_USERNAME`（待办提醒）。用法见 [部署](deployment.md#飞书集成可选)。

## 仅 Docker Compose 使用

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | 必填 | 宿主机开发时 `PG_DSN` 中的密码须与之相同 |
| `POSTGRES_USER` / `POSTGRES_DB` | `lawagent` | |
| `S3_ACCESS_KEY` | `crosscomply` | SeaweedFS 的访问账户；容器内后端的 `CROSSCOMPLY_S3_ACCESS_KEY` 取自此值 |
| `S3_SECRET_KEY` | 必填 | 不能含引号或反斜杠 |
| `S3_BUCKET` | `crosscomply-materials` | |
| `SEAWEEDFS_IMAGE` | `chrislusf/seaweedfs:3.80` | |
| `SEAWEEDFS_VOLUME_SIZE_LIMIT_MB` / `SEAWEEDFS_VOLUME_MAX` | `64` / `20` | 总容量约为两者之积；不要去掉上限，否则可能占满磁盘 |
| `CROSSCOMPLY_HTTP_PORT` | `8080` | nginx 对外端口 |
| `CROSSCOMPLY_CORPUS_DIR` | `./data/corpus/legal_docs_20260702` | 挂载到后端的语料目录 |
| `CROSSCOMPLY_ENV_FILE` | `.env` | Compose 的通用后端环境文件 |
| `ES_JAVA_OPTS` | `-Xms512m -Xmx512m` | |
| `NPM_REGISTRY` | `https://registry.npmjs.org` | 前端镜像构建使用的 npm 源 |

## 数据治理流水线

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LAWAGENT_DOCLING_OCR_ENGINE` | `rapidocr` | `kserve_v2_ocr` 时改用远程 OCR |
| `LAWAGENT_DOCLING_OCR_API_URL` | — | 远程 OCR 地址 |
| `LAWAGENT_DOCLING_OCR_MODEL_NAME` / `_MODEL_VERSION` / `_TRANSPORT` / `_TIMEOUT` | `ocr` / — / `http` / `60` | |
| `LAWAGENT_DOCLING_OCR_HEADERS` / `_REQUEST_PARAMETERS` | — | JSON 对象 |
| `LAWAGENT_DOCLING_ARTIFACTS_PATH` | `data/models/docling` | Docling 模型缓存 |

## 前端

前端变量由 Vite 从 `frontend/` 目录的 `.env*` 读取，根目录 `.env` 对它无效。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | 空（同源 `/api`） | 开发时 Vite 已把 `/api` 代理到 `127.0.0.1:8000` |
| `VITE_PUBLIC_DEMO` | `false` | `--mode demo` 构建时强制为 true |
