# CrossComply

[![CI](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/raydemo1/crosscomply-agent/actions/workflows/ci.yml)

CrossComply 是面向企业数据出境合规的审查工作台。核心是一个有状态、可暂停恢复的 Compliance Agent：它阅读冻结的案件材料，在受控法律库中反复检索和精读，必要时在官方网站做受限调查，最后提交必须通过引用门禁和独立语义校验的审查报告。证据不足时报告会明确说明缺口，而不是猜测。审批、整改等对外动作只能由人完成。

## 功能概览

| 模块 | 说明 |
| --- | --- |
| 案件审查 | 新建案件、上传材料、确认申报事实，由 Agent 出具带引用的审查报告；Agent 可暂停向申请人追问 |
| 案件详情 | 报告、事实核对、原文批注、修改建议、案件内问答、PDF 决策报告 |
| 合规问答 | 不建案件的独立问答，流式返回，可转为正式案件 |
| 整改与审批 | 整改计划与证据提交、飞书审批（终态只由验签后的飞书事件写回） |
| 待办与业务 | 个人待办、持续出境业务及其关联案件、飞书提醒 |
| 知识库 | 法律法规与规章制度的导入、版本更新、回收站；Agent 发现的新官方来源需人工复核后入库 |

## 快速开始

需要 Python 3.11+、Node.js 22.12+、Docker。

```bash
pip install -e ".[dev,service]"
cp .env.example .env        # 填入数据库/MinIO 密码与模型密钥，见 docs/configuration.md
docker compose up -d --build
```

首次部署还需要创建管理员并建立检索索引，见 [部署](docs/deployment.md)。只做本地开发（宿主机运行 API、Worker 和 Vite）见 [开发指南](docs/development.md)。

## 测试

```bash
pytest
cd frontend && npm test && npm run build
```

CI（`.github/workflows/ci.yml`）在每次推送和 PR 上运行同样的检查。

## 文档

| 文档 | 内容 |
| --- | --- |
| [架构](docs/architecture.md) | 系统组成、Agent 的动作与预算、程序保证、人工职责、状态机、代码地图 |
| [开发指南](docs/development.md) | 本地环境、语料恢复、启动各进程、测试与迁移约定 |
| [部署](docs/deployment.md) | Docker Compose 拓扑、首次初始化、飞书配置、日常运维、公开演示站 |
| [配置参考](docs/configuration.md) | 全部环境变量及默认值 |
| [数据治理](docs/data-governance.md) | 法律知识如何进入受控证据库、引用资格规则、分块与解析器 |
| [评测](docs/evaluation.md) | 检索 benchmark 与生产 Agent 评测的入口、指标和报告口径 |

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `law_agent/review/` | 生产 Agent runtime、Worker、FastAPI（`http/` 为路由）、检索、评测集 |
| `law_agent/kb/` | 在线知识库入库、补库核验 Worker |
| `law_agent/data/` | 离线数据治理流水线 |
| `law_agent/llm/` | OpenAI 兼容客户端、Embedding、重试预算 |
| `frontend/` | React + Vite 工作台；`src/demo/` 为公开演示站的虚构数据 |
| `alembic/` | 数据库迁移 |
| `docker/` | 镜像、nginx 配置、报告字体 |
| `scripts/` | `transfer_corpus.py`（语料打包/恢复）、`setup-dev.ps1`（生成本地 `.env`）、`run_agent_baseline.py`（Agent 评测实跑） |
| `tests/` | pytest 测试 |

`data/`（语料、模型缓存、评测产物）、`output/`、`tmp/` 不进 Git。

## 许可

[MIT](LICENSE)
