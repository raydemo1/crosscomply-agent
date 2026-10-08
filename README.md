# CrossComply — Cross-Border Data Compliance Agent

[![CI](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml)

CrossComply 是面向企业数据出境合规执行的单 Agent 系统：一个有状态、可暂停恢复的 Compliance Agent 自主阅读冻结材料、重复检索受控法源、在官方来源中做受限 Web 调查，提交经引用门禁与独立语义校验的报告；检索不满足时披露 evidence gap 而不是猜测。审批与整改等外部副作用只能由人完成。

## 文档

| 文档 | 回答的问题 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 系统现在如何运行：Agent / 程序 / 人各自的边界，生产 runtime 与 benchmark 流水线的区别 |
| [docs/data-governance-design.md](docs/data-governance-design.md) | 法律知识如何进入受控证据库，什么时候具备条款引用资格 |
| [docs/SERVICE_STACK.md](docs/SERVICE_STACK.md) | 部署依赖、服务拓扑、启动与运维事实 |

## 开发启动

```powershell
# 1. 依赖与环境（Python 3.11+）
pip install -e ".[service]"
Copy-Item .env.example .env   # 填入 LLM / Embedding key 与 POSTGRES_PASSWORD、MINIO_ROOT_PASSWORD

# 2. 起服务栈（PostgreSQL+pgvector、Elasticsearch、MinIO、API、Worker、knowledge-worker、前端、nginx）
docker compose up -d --build

# 3. 首次部署：创建管理员，再索引语料并体检
docker compose run --rm -e CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD="<至少12位强密码>" `
  api python -m law_agent.review.bootstrap_admin --username admin@example.com --display-name "系统管理员"
docker compose exec -T api python -m law_agent.review index-service --execute
docker compose exec -T api python -m law_agent.review service-doctor
```

完整部署（含飞书审批、统一入口 8080、迁移与数据卷）见 [docs/SERVICE_STACK.md](docs/SERVICE_STACK.md)。

仅做前后端本地开发时，可只起 `postgres` 和 `elasticsearch`，然后：

```powershell
python -m law_agent.review serve --host 127.0.0.1 --port 8000   # 后端，/docs 为 OpenAPI
cd frontend; npm install; npm run dev                            # Node >=22.12.0，http://127.0.0.1:5173
```

## 测试

```powershell
pytest
cd frontend; npm test
```

## 质量验证入口

两套评测长期共存，回答不同问题，不共用指标结构：

| | 测什么 | 入口 |
| --- | --- | --- |
| **Retrieval benchmark** | 固定检索流水线（`service.py`）的搜索召回 | `python -m law_agent.review eval` |
| **Production Agent Eval** | 交付给用户的生产 Agent（`agent_runtime.execute_agent_task`）端到端表现：完成/abstain、必须法源、非法条款引用、freshness、预算行为、追问质量 | `python -m law_agent.review agent-eval` |

### Production Agent Eval

直接运行时（冻结 MaterialSnapshot/IntakeSnapshot/ReviewTask + InMemoryEnterpriseStore），真实经过 Agent 决策、引用门禁与独立语义校验；支持 `request_input → waiting_input → 预设人工回答 → resume` 场景。指标分两层：

- **确定性指标**（无需另一个模型）：是否完成、abstain 是否正确、必须法源是否覆盖、是否把标准/指南当条款引用、freshness hold、turns/searches/reads/web、追问次数、预算耗尽、workflow failure；
- **语义 judge**（仅评测使用，不进生产链路）：`legal_correctness / exception_coverage / fact_grounding / clarification_quality / overall_pass`，默认与受测 Agent 同模型，可用 `--judge-model` 指定不同模型；`--no-judge` 只跑确定性检查。

每个案例的总评是三态：`PASS`（已评测通过）/ `FAIL`（已评测失败，含 workflow failure）/ `UNEVALUATED`（judge 基础设施故障——既不算 Agent 失败，也绝不算通过）。

```powershell
python -m law_agent.review agent-eval --suite smoke --no-judge
python -m law_agent.review agent-eval --suite core --judge-model <judge-model> `
  --output data/review_runs/agent_eval_core.json --report data/review_runs/agent_eval_core.md
```

> 当前状态：框架（harness、指标、judge、CLI、JSON/Markdown 报告）已就绪，`smoke` 含 2 个自检案例——一个提供 scripted answer 用于覆盖 `request_input → resume` 路径（该回答仍属未核实 applicant statement，恢复后以证据不足诚实收口；rubric 不强制 Agent 必须追问，直接谨慎 abstain 同样合格），一个 out-of-corpus 正确 abstain；15–20 个人工 golden cases（`core`）在下一步补齐后才有第一批在库指标。Rubric 只描述"必须判断对什么"，不规定执行步骤。

### Retrieval benchmark

`law_agent/review/evalset` 维护冻结的场景集，跑的是 `service.py` 的固定检索流水线，衡量**检索层**召回质量，不代表 Production Agent 的端到端结论正确率。

| Metric | LLM | LLM + rerank |
|---|---:|---:|
| Recall@5 | 87.06% | 87.72% |
| Must-have Recall@5 | 89.04% | 89.91% |
| Optional coverage@5 | 82.14% | 75.00% |

口径：82 个冻结场景、冻结事实与查询输入、真实 service 检索、DeepSeek-V4-Flash、人工复核的核心/辅助法源标签（Must-have 覆盖 76 个含核心法源场景，Optional 覆盖 28 个需指南/模板/Q&A/国标补充的场景）。

```powershell
python -m law_agent.review eval --suite quick --max-workers 8 --output data/review_runs/eval_quick_service.json --report data/review_runs/eval_quick_service.md
python -m law_agent.review eval --suite full  --max-workers 8 --output data/review_runs/eval_full_service.json  --report data/review_runs/eval_full_service.md
```

> 评测会产生模型调用成本。除需要更新对外指标外，日常开发不重跑全量评测。
> Production Agent 的端到端评价目前**没有**在库指标；不要用上表推断 Agent 整体正确率。

## 常用入口

```powershell
python -m law_agent.kb ingest .\新资料.pdf     # 新知识入库（先不可检索，校验后原子切换）
python -m law_agent.data pipeline run          # 离线数据治理流水线（manifest→fetch→normalize→clean→enrich→chunk）
```

数据治理流水线、解析器选择与引用资格规则见 [docs/data-governance-design.md](docs/data-governance-design.md)。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `law_agent/review/` | 生产 Agent runtime（`agent_runtime.py`、`agent.py`、`agent_tools.py`）、Worker、FastAPI、检索与离线评测 |
| `law_agent/data/` | 数据治理流水线：manifest、采集、解析、清洗、语义增强、分块、引用资格策略 |
| `law_agent/kb/` | 在线知识库入库与补库核验 |
| `frontend/` | React + Vite 案件工作台 |
| `alembic/versions/` | 数据库迁移 |
| `tests/` | pytest 测试 |

`data/`（语料包、模型缓存、评测产物）默认被 git 忽略。
