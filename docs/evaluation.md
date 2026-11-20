# 评测

项目有两套互相独立的评测，回答不同的问题，指标不能混用。

| | 测什么 | 入口 | 数据 |
| --- | --- | --- | --- |
| 检索 benchmark | `service.py` 固定检索流水线的法源召回 | `python -m law_agent.review eval` | `law_agent/review/evalset/cases*.py`、`manual_labels.py` |
| 生产 Agent 评测 | `agent_runtime.execute_agent_task` 的端到端交付 | `python -m law_agent.review agent-eval` | `law_agent/review/evalset/agent_cases*.py` |

两者都会调用付费模型（`--no-judge` 也仍然调用受测 Agent），日常开发不要跑全量；离线单元测试见 `pytest`。

## 检索 benchmark

法规影响的离线演示（不调用模型）：`python -m scripts.demo_regulation_change --cases 20`。它只用内存和临时语料，人为设置旧问答的失效日期，并不表示真实法规已废止该问答。构造集分别核对标记的精确率和召回率，以仅改标题或 URL 为负例；同规模多次运行记录耗时 P50。不要把构造集指标当作线上准确率。

82 个冻结场景（冻结事实与查询输入），对真实 ES + pgvector 检索，用人工复核的核心 / 辅助法源标签计分。

```bash
python -m law_agent.review eval --suite quick --max-workers 8 \
  --output data/review_runs/eval_quick.json --report data/review_runs/eval_quick.md
python -m law_agent.review eval --suite full --max-workers 8 \
  --output data/review_runs/eval_full.json --report data/review_runs/eval_full.md
```

最近一次对外指标（DeepSeek-V4-Flash，82 场景）：

| 指标 | LLM | LLM + rerank |
| --- | ---: | ---: |
| Recall@5 | 87.06% | 87.72% |
| Must-have Recall@5（76 个含核心法源的场景） | 89.04% | 89.91% |
| Optional coverage@5（28 个需辅助材料的场景） | 82.14% | 75.00% |

Recall@5 是前 5 条命中中的来源覆盖率，逐案计算后取平均。它不代表法条适用是否正确、整案结论是否正确或生产 Agent 的完成率。

## 生产 Agent 评测

直接运行生产 runtime（冻结快照 + `InMemoryEnterpriseStore`），真实经过 Agent 决策、引用门禁和语义校验，并支持 `request_input → waiting_input → 预设回答 → resume` 场景。

```bash
python -m law_agent.review agent-eval --suite smoke --no-judge
python -m law_agent.review agent-eval --suite core --judge-model <模型> \
  --output data/review_runs/agent_eval_core.json --report data/review_runs/agent_eval_core.md
```

### 案例集

| 集合 | 位置 | 用途 |
| --- | --- | --- |
| `smoke` | `agent_cases.py` | 2 个框架自检案例 |
| `core` | `agent_cases_core.py`、`agent_cases_second.py` | 17 个合成候选，已参与错误分析和修复，作为**回归集** |
| `portfolio` | `agent_cases_portfolio.py` | 3 个合成候选，覆盖跨档、接近安全评估门槛和上年度记录 |
| holdout | `agent_cases_holdout.py` | 6 个未用于调优的合成候选，不随 `core` / `smoke` 运行，只能通过选择清单运行 |

全部案例的 `review_status` 都是 `candidate`：它们是基于业务场景构造的合成题，没有经过独立专家的人工法律审定。逐案 AI 复核记录保存在 `law_agent/review/evalset/agent_case_review.json`。

### 指标

主体累计诊断使用相同模型、语料与 rubric，分别运行 `agent-eval --suite portfolio` 和带 `--without-portfolio` 的同套用例。单独报告漏提示累计风险、把未去重合计当作确定人数，以及把上年度记录当作零人的情况；该结果不代表线上质量。

- **确定性指标**（不需要 judge）：是否完成、abstain 是否正确、必须法源是否覆盖、是否把标准/指南当条款引用、freshness hold、轮数/检索/读取/Web 次数、追问次数、预算耗尽、运行失败。
- **语义 judge**（仅用于评测，不进生产链路）：`legal_correctness`、`exception_coverage`、`fact_grounding`、`clarification_quality`、`overall_pass`。默认与受测 Agent 同模型，用 `--judge-model` 指定其他模型；`--no-judge` 只跑确定性检查。judge 把明确违反 rubric 的内容记入 `rubric_violations`，任何维度 fail 或存在违规时总评不能为 PASS。

每个案例的总评有三种：`PASS`、`FAIL`（含运行失败）、`UNEVALUATED`（judge 故障或模型服务阻断，例如余额不足；既不算通过也不算失败）。

### Rubric

每个案例只记录判断边界，不规定搜索次数、阅读顺序或必须调用的工具：

- `allowed_judgments` / `forbidden_judgments`：允许与禁止的最终法律判断（不能用 `risk_level` 代替）；
- `must_cover_sources` / `forbidden_clause_sources`：必要的法律依据、不能作为条款引用的来源；
- `required_exceptions`、`must_not_assume`、`clarification_expectations`：例外条件、不能擅自认定的事实、必要澄清点；
- `should_abstain`、`expect_freshness_hold`：收口边界；`should_abstain=null` 时由 judge 按允许/禁止判断评估条件性报告；
- `selection_reason`、`reference_basis`：该题针对的错误类型和法源核对记录。

题目优先通过 `source_case_id` 复用检索 benchmark 的问题与材料，但不复用其法源标签。

### 人工审定包

不调用模型的审定包，包含题目、材料、确认事实、预设回答、rubric、法源依据和逐案散列：

```bash
python -m law_agent.review.evalset.agent_review --output-dir data/review_runs/agent_review_package
python scripts/run_agent_baseline.py --selection-plan data/review_runs/agent_review_package/selection.json --dry-run
```

已存在的输出目录不会被覆盖。审定者逐案记录 `approved / revise / rejected`；审定记录绑定案例散列，题目或 rubric 修改后必须重新审定。程序不会代填审定身份或自动把案例升级为 `approved`。

实跑时去掉 `--dry-run`，使用新的 `--output-dir` 和 `--workers 1`。运行目录逐案即时落盘（`manifest.json` 记录模型、配置、代码与语料散列；每个案例保存事件、状态、最终交付和 judge 输入），遇到认证、余额或限流阻断时停止启动后续案例。

## 报告口径

- 先列总案例数、人工审定数、实际运行数、未完成评测数；小样本用 `n/N`，并写明版本、运行次数、评测者和 judge 模型。
- 法律结果（正确确定判断 / 正确条件性判断 / 错误确定判断 / 其他法律失败）与执行结果（正常交付 / 预算耗尽 / 运行故障 / 未启动）分开统计。预算耗尽后的 abstain 即使安全，也不算正常交付。
- 不同版本、不同 rubric、定向挑选的复测不能拼成总体通过率；回归集与留出集分别报告。重复运行要预先确定次数并报告全部结果。
- 未经人工审定的候选 PASS 不能称为"法律准确率"；检索召回也不能用来推断 Agent 的法律正确率。
- 失败分类可多选：法律推理、遗漏例外、事实 grounding、法源覆盖、引用、澄清、abstention、freshness、预算耗尽、运行/评测设施。缺少法源时结合 checkpoint 区分"没找到 / 找到没采用 / 采用但理解错"。rubric 有误或 judge 错判记为评测问题，不通过修改生产 Agent 迎合题目。

