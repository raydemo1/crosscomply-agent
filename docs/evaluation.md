# 评测

评测系统在 `law_agent/evals/`，不属于生产代码。它回答两个问题，指标不能混用：

| 问题 | 怎么测 | 命令 |
| --- | --- | --- |
| 给定合理的查询，生产检索能不能找出**对的条款**？时效或地域不对的条文会不会混进来？ | 固定查询直接调用 `ComplianceAgentTools.search`，按（来源，条号）计分 | `retrieval` |
| 同样的材料，CrossComply 的结论比通用模型、通用联网 Agent 更可靠吗？ | 多个被测系统跑同一批场景，用同一套与系统无关的评分 | `agent` |

只有 `check`、`review-package`、`record-review`、`lock-test`、`export-prompts`、`report` 不调用模型；`retrieval` 调用 embedding；`agent` 调用付费模型，日常开发不要跑。

## 结构

| 模块 | 职责 |
| --- | --- |
| `bank/` | 场景库：结构定义（`schema.py`）、加载与材料落盘（`loader.py`）、对照语料的确定性检查（`checks.py`）、数据（`data/`） |
| `arms/` | 被测系统：`crosscomply`（生产 runtime）、`direct`（同模型直接作答）、`web`（同模型 + 开放联网搜索，无门禁）、`imported`（导入人工在商用助手中跑出的答案） |
| `delivery.py` | 各系统统一的最终交付格式 |
| `grading.py` | 引用解析、确定性检查、评测专用 judge、结果分类 |
| `retrieval.py` | 检索评测 |
| `runs.py` | 运行留证（manifest、逐案落盘、阻断即停）与 Wilson 区间 |
| `review.py` | 人工审定包、审定记录、test 集锁定 |
| `report.py` | 报告 |
| `field.py` | 线上纠错草稿导出为 field 场景 |

## 场景库

一个场景（`bank/schema.py::Scenario`）包含：

- **审查时点** `as_of_date`（必填），保证时效判断可复现，不随运行日期漂移；
- **材料包** `materials`：多个文件，文本文件用 `text`，XLSX 用 `sheets`。运行时按真实上传路径解析，XLSX 会按工作表转换；
- **事实模式出处** `fact_pattern_source`：类型为 `enforcement`（执法案例）、`approved_case`（获批案例）、`official_qna`（官方问答）、`synthetic`（合成）、`field_feedback`（线上纠错）。必须分开写明公开来源写了什么、哪些是补充或虚构的。主体一律使用虚构名称；
- **企业画像** `persona_id`（`bank/data/personas.json`），同一主体可以出现在多个场景中，用于测试同主体累计；
- **判断点** `judgment_points` 与**噪声类型** `noise_types`，用于覆盖统计；
- 两层标签，至少要有一层：
  - `retrieval`：1–4 条固定查询，以及 `must_cite`（可引用条款，必须标到条号）、`must_retrieve`（必须检索到但不能作为条款引用的指南、问答、清单）、`optional`、`must_not_cite`（该时点已失效、被取代或地域/行业不适用的条文）；
  - `agent_rubric`：允许与禁止的判断、必须核对的例外、不能擅自认定的事实、需要澄清的点，以及必须覆盖或禁止作为条款引用的来源；
- 可选的 `portfolio`（冻结的同主体累计台账，注入方式与生产相同）、`scripted_answers`（Agent 暂停追问时的预设回答）、`controlled_web`（把来源从语料中留出，并提供固定的官方 Web 结果）；
- `answer_leak_sources`：语料中直接给出答案的来源（如政策问答），报告中单独统计。

`split` 取值为 `dev`（可以用于调优）、`test`（锁定后只在正式出报告时运行）、`field`（线上纠错，存放在 Git 之外的 `data/evals/field/`）。

```bash
python -m law_agent.evals check            # 对照当前语料检查全部场景并输出覆盖统计
python -m law_agent.evals check --no-corpus
```

`check` 检查的内容：申报字段合法；标签中的来源和条号存在；`must_cite` 在审查时点可以作为条款引用（复用 `retrieval/temporal.py` 的规则）；可引用的条款没有被错放进 `must_retrieve`；标为已被取代的版本在该时点确实已失效；rubric 填写完整；审定记录与场景散列一致；锁定的 test 集没有被改动。法律判断是否正确，仍由人工审定决定。

## 人工审定与 test 集锁定

```bash
python -m law_agent.evals review-package --split test --output-dir output/review-test
# 审定人在 review.json 填写 decision、reviewer、reviewed_at 和理由
python -m law_agent.evals record-review output/review-test/review.json
python -m law_agent.evals lock-test
```

审定记录（`bank/data/reviews.json`）绑定场景散列，场景改动后必须重新审定。程序只复制人工填写的结论，不会代填审定人，也不会自动升级为 `approved`。`lock-test` 默认要求 test 集全部审定通过；锁定后，任何 test 场景被修改，`check` 都会报错。

## 检索评测

```bash
python -m law_agent.evals retrieval --output-dir data/eval_runs/retrieval_<日期>
```

指标：

- **条款召回@5 / @10**：`must_cite` 的（来源，条号）出现在前 k 条结果中的比例；
- **必检来源召回@10**：`must_retrieve`；
- **命中排除条款的场景数**：前 k 条中出现了可引用的 `must_not_cite` 条文。

结果按 split、判断点分别统计，并单独给出"不含直接答案场景"一栏。这里只衡量检索质量，查询写得好不好要看 Agent 评测中的检索日志。

## 被测系统对照

```bash
python -m law_agent.evals agent --split dev --arm crosscomply --arm direct --arm web \
  --judge-model <模型> --output-dir data/eval_runs/agent_<日期>
python -m law_agent.evals export-prompts --split test --output-dir output/prompts   # 人工在商用助手中运行
python -m law_agent.evals agent --split test --arm crosscomply --import assistant=output/answers \
  --output-dir data/eval_runs/agent_test_<日期>
python -m law_agent.evals agent --split dev --without-portfolio --output-dir ...    # 同主体累计台账的消融对照
```

- 所有系统使用同一个底座模型（`--model`，默认为 `OPENAI_COMPATIBLE_MODEL`），差别只在系统设计；
- 非 CrossComply 系统给出的法律名称会按标题解析回语料中的 `source_id`，解析不了的计入"无法解析的引用"；
- CrossComply 默认跳过独立语义校验（`--verifier skip`），直接评 Agent 首次生成的报告；引用门禁仍然生效。`--verifier on` 走完整生产链路。
- judge 只看交付内容，不知道交付来自哪个系统。它先判定结论形态（确定 / 附条件 / 不下结论），再按 rubric 逐维评分；默认与被测模型相同，建议用 `--judge-model` 换一个模型。

**结果分类**：法律结果与执行结果分开统计。

| 法律结果 | 含义 |
| --- | --- |
| `correct_definite` / `correct_conditional` / `correct_abstain` | 通过确定性检查和 judge |
| `wrong_definite` | **危险错误**：给出了违反判断边界的确定结论 |
| `other_legal_failure` | 其他不通过（附条件或不下结论但有错误） |
| `unevaluated` | judge 或模型服务故障，不计入分母 |

执行结果分为 `delivered`、`budget_exhausted`（预算耗尽后的收口，即使安全也不算正常交付）、`runtime_failure`、`blocked`（认证、余额、限流等）、`unanswered_gate`（追问没有对应的预设回答）。

**运行留证**：运行目录不会覆盖已有目录。`manifest.json` 记录代码提交、工作区是否有未提交改动、生产代码散列、语料散列、各系统配置和场景散列。每个系统 × 场景一个目录，保存 `delivery.json`、`grade.json` 和 `events.jsonl`（CrossComply 的每个检查点都会写入）。遇到 401/402/403/429 时，停止启动后续场景。`python -m law_agent.evals report <运行目录>` 可以重新生成报告。

## 线上纠错回流

在治理中心编辑现场候选，人工检查脱敏后的材料、问题、申报信息和判断标准，再标为就绪。然后执行：

```bash
python -m law_agent.evals field-export --dry-run
python -m law_agent.evals field-export
```

导出的 field 场景写入 `data/evals/field/`（不进 Git），状态保持 `draft`，与合成场景分开报告。已导出的场景文件如果被改动，再次导出会报错，不会覆盖。

## 法规影响演示

`python -m scripts.demo_regulation_change --cases 20` 只使用内存和临时语料，人为设置旧问答的失效日期，并不表示真实法规已废止该问答。它报告标记的精确率、召回率和耗时，构造集上的指标不代表线上准确率。

## 报告口径

- 先列场景数、已评定数和审定状态；小样本写成 `n/N`，附 95% Wilson 区间。
- dev、test、field 分开报告；`draft` 场景的通过不能称为法律准确率；有直接答案的场景单独列出。
- 不同版本、不同 rubric、定向挑选的复测不能合并成一个总体通过率；重复运行的次数要预先确定，并报告全部结果。
- 检索召回不能用来推断 Agent 的法律正确率。
- rubric 有误或 judge 判错，记为评测问题，不通过修改生产 Agent 去迎合题目。
