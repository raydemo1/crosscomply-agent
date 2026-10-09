# Production Agent Golden Set（Slice 2B）

## 目标与证据边界

评估 `agent_runtime.execute_agent_task` 在高风险法律判断中的可靠与不可靠之处，并留下可读、可复核的失败原因。检索 benchmark 继续独立存在，其召回标签不能直接作为 Agent 的法律结论标签。

本批为基于现有业务场景构造的**合成候选案件**，不是已经人工审定的真实客户案件。每个案例的 `review_status` 当前均为 `candidate`。查阅官网和本地法条、通过程序校验或获得模型 judge 的 PASS，都不能替代人工法律审定。只有完成逐案审定后，才能将对应案例改为 `approved`，并据此报告 Golden 指标。

当前有17个候选：首轮8个，第二轮9个。先跑代表案例，再按实际失败补第二轮，不以业务领域覆盖数量作为目标。建立基线时冻结生产代码；用户授权修复已定位的Agent问题后，保留修复前基线，修复与复测使用新目录，不把两个版本的结果混算。

## 回归集、留出候选与人工审定

现有17案已经参与错误分析、prompt及报告反馈修复，定位为回归集。修好后继续保留原始判断约束，用于检查换模型、改工具及改提示词后的退步；通过这些案例不能证明未见案件的泛化。

另准备6个未实跑的合成候选，独立保存在`agent_cases_holdout.py`，不加入`core`或`smoke`模型入口：

| 候选 | 待审定的新增判断边界 |
| --- | --- |
| `holdout_foreign_origin_001` | 境外来源数据经境内处理后返还的例外 |
| `holdout_mixed_origin_001` | 加工引入境内信息后，不能把整批数据自动认定为同一例外 |
| `holdout_personal_booking_001` | 个人为合同当事人且出境确为履约所必需 |
| `holdout_business_contract_001` | 企业服务合同不能代替个人合同例外 |
| `holdout_nonpersonal_trade_001` | 确认不含个人信息或重要数据的贸易数据 |
| `holdout_pseudonymous_data_001` | 编码化措辞不能推翻已确认的个人信息属性 |

选题参考历史缺陷，按例外范围与事实对象设计三组对照，不仅替换人数。它们不是独立机构编制的盲测题、真实客户案件或已经审定的Golden。审查时点固定为2024-03-23，候选依据核对[2024规定官方正文](https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm)，不把它当成2026最新法源发现测试。人工审定还须核对该时点的完整适用关系和题目事实；如用于调优，转入回归集，不能继续宣称留出。

免费离线准备命令：

```powershell
python -m law_agent.review.evalset.agent_review --output-dir data/review_runs/agent_review_package
```

导出`cases.json`、`review.md`、`review.json`和`manifest.json`。输入与填报类型、重复案例和rubric完整性在本地校验；不调用Agent、judge、服务检索或Web。审定记录初始均为`pending`，包含逐案SHA256、审定者、日期和理由。生产Python散列仅记录当前版本，正式实跑仍须另行冻结模型配置、语料及评测版本，不能把审定包当运行结果。

审定者应逐案核对事实前提、允许与禁止判断、例外、必要澄清和关键法源，记录`approved / revise / rejected`。审定记录须绑定当前案例散列，修改材料、答案或rubric后重新审定。程序不会代填审定身份、自动升级案例或将本地校验当作法律审定；正式采用时将审定记录与对应案例一起纳入Git。没有外部或独立专业复核时，应披露仍是作者自审。

## 23案复核与增量运行

2026-10-09完成全部23案的AI逐案复核，核对问题、材料、填报、允许/禁止判断、例外、关键法源及既存运行记录。持久记录见[agent-case-review.json](agent-case-review.json)，包括逐案散列、复核结论、来源、修正内容、历史结果路径与本次是否需要运行。复核身份明确为Codex，独立专家人工审定仍未完成，不将源码的`candidate`升级为`approved`。

发现并处理了三处题目问题：

- HR正例原有3000人，同时满足小量普通信息免予，不能隔离HR例外能力。修改为当年累计12万人、材料与填报一致，保留HR必要性、劳动规章制度和集体合同。原PASS报告一概要求单独同意，遗漏[个人信息保护法第十三条](https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm)的非同意处理依据；[2026年7月官方问答第1问](https://www.cac.gov.cn/2026-07/24/c_1786638883119336.htm)明确此类出境无需取得个人同意、仍需履行告知。补充rubric后，旧PASS不能验证新题。
- 小量敏感信息案例的材料为商业分析供应商，填报误写客服供应商，现已统一填报；该输入变化需重跑。
- freshness题的法源核对记录误用普通数量场景模板，改为实际汽车指引发布通知。只改法源说明，未改材料、rubric或受控Web fixture，不因此追加模型调用。

HR正例与HR缺口案例人数不同，不能作为只改变集体合同一个变量的受控实验。敏感信息案例已在填报提供敏感属性，评测义务理解与来源角色，不以其PASS证明独立分类能力。库外欧盟案例依赖受控语料缺少该法域的前提；freshness案例依赖完整来源留出与固定Web返回，两者均不构成线上搜索能力指标。TC260官方PDF本次抓取返回502，引用角色已核对本地manifest与GB/T官方页面，未宣称重新核验该PDF全文。

后续只选10案：

| 类型 | 案例 |
| --- | --- |
| 既存法律失败 | `agent_hr_exception_missing_001` |
| 既存执行异常 | `agent_confirmed_material_conflict_001` |
| 本次修正后的输入/评分边界 | `eval_standard_contract_003`、`agent_small_sensitive_001` |
| 新增未实跑候选 | 6个`holdout_*`案例 |

前两案已有不同生产快照的定向成功记录，但保留为已发生失败的复测题，不能用旧成功拼成现版本完整分数。其余13案判断边界未变且曾正常通过，本批跳过；历史结果保持原样，本次没有调用Agent或judge。

离线导出会保存`ai_review.json`与`selection.json`。清单绑定案例和复核记录散列，题目改动后重新复核、导出，防止静默使用旧清单。免费验证：

```powershell
python -m law_agent.review.evalset.agent_review --output-dir data/review_runs/agent_review_package
python scripts/run_agent_baseline.py --selection-plan data/review_runs/agent_review_package/selection.json --dry-run
```

实际模型运行时使用同一`--selection-plan`、新`--output-dir`与`--workers 1`，去掉`--dry-run`；会调用付费模型，应在另行授权的运行轮执行。本清单只是选题，正式运行仍记录当次生产代码、模型配置和语料。仅报本次10案结果，不能与跳过的13案历史PASS相加为当前23案通过率，也不能声称获得现版本完整回归基线。

## 简历与面试的指标口径

检索和法律判断分开报告。检索Recall@5按前5条命中中的来源覆盖计算逐案比例，再对有标签场景取平均；Must-have Recall@5只计算有核心法源标签的场景。它们不代表法条适用正确率、整个案件准确率或生产Agent完成率。引用可定位也不等于引用足以支持判断。

正式端到端报告先列总案例数、人工审定数、实际运行数和未完成评测数。法律结果按正确确定判断、正确条件性判断/合理保留、错误确定判断及其他法律失败分类；执行结果另列正常交付、预算耗尽、运行故障和未启动。法律类别必须通过原rubric和人工复核判断，不能用`risk_level`自动推导。预算耗尽后的系统abstain即使安全，也不能记为正常交付。未评测不能算通过、算法律失败或隐去。

样本较小时优先给出`n/N`，同时披露分母、版本、实跑次数、评测者与judge型号；不同版本、不同rubric及定向挑选复测不能拼成总体通过率。重复运行须预先确定次数、报告全部结果，不挑最好的一次；费用和耗时只使用实际记录。留出集和回归集分别报告，受控Web留出另列，不能证明线上Web召回。

目前只有历史候选评分和定向复测，不存在新的23案实跑结果或人工审定准确率。17案修复后历史计数为16 PASS、1 FAIL，其中含预算耗尽后的系统abstain及人工待复核的评分分歧；不写成“法律准确率94.1%”。

简历可表述为：“维护冻结业务场景的检索评测，建立高风险Agent候选评测与回归集，结合执行轨迹定位并修复法源关系、事实来源冲突及条件未知时的判断收口问题。”检索百分比引用对应冻结实验，说明范围；尚未实跑、未审定的新增候选不作为能力提升指标。面试中明确自建题的用途与局限，重点解释具体失败、定位证据、通用修复与验证边界，而非追求自建集全通过。

## 案例与轻量 rubric

题目和原材料优先通过 `source_case_id` 复用 retrieval eval，不复制其 `expected_sources`、`should_abstain` 或法律判断。材料冲突、历史时点等 Agent 能力需要新增材料；审校发现原输入不能隔离目标能力时，可修正Agent候选材料并保留旧运行。已确认事实通过与生产相同的冻结 intake 提供。评测事实是合成输入，不能声称来自真实审核记录。

每个 rubric 只记录：

- `allowed_judgments` / `forbidden_judgments`：允许与禁止的最终法律判断，不限定句式。不能用 `risk_level` 代替法律路径正确性。
- `must_cover_sources` / `forbidden_clause_sources`：真正必要的法律依据与不能作为法律条款引用的来源；不要求穷举全部检索标签。
- `required_exceptions`、`must_not_assume`、`clarification_expectations`：例外条件、禁止擅自确认的事实、必要澄清点。
- `should_abstain`、`expect_freshness_hold`：无法确定判断或新法源尚待治理时的收口边界。`should_abstain=null` 时不强制风险等级标签，由 judge 按允许/禁止的判断评估条件性报告；未运行 judge 时不形成总评。

`selection_reason` 说明该题检验的错误类型，`reference_basis` 保留法源与条款的核对记录。rubric 不规定搜索次数、阅读顺序或必须调用某个工具。缺口已被准确披露且诚实 abstain 的交付，不因没有 `request_input` 自动判错。预设回答保留 `applicant_statement` 等 provenance，不伪装为已核实事实。

## 首轮候选

| 案例 | 复用基础 | 判断能力 |
| --- | --- | --- |
| `eval_cross_border_008` | 原问题、原材料，补充已确认 intake | 现行12万人普通个人信息出境机制、统计期间及旧规则误用 |
| `eval_standard_contract_003` | 原问题、原材料，补充已确认 HR 免予事实 | 第五条 HR 例外，免予机制与剩余义务的区分 |
| `eval_cross_border_001` | 原问题、原材料，未核实预设补充 | 日均处理量与累计出境人数，追问回答 provenance |
| `eval_shanghai_001` | 原问题、原材料 | 地方清单的地域、行业与数据适用范围缺失 |
| `eval_sensitive_002` | 原问题、原材料，补充已确认事实 | 敏感信息与参考标准的证据角色 |
| `agent_material_conflict_001` | `eval_cross_border_008`，新增冲突材料 | 同口径12万与120万矛盾，不能自行选数值 |
| `agent_historical_001` | `eval_cross_border_008`，新增历史材料 | 2023-12-31有效规则，不追溯套用2024门槛 |
| `eval_out_of_corpus_001` | 原问题、原材料 | 库外法域应当 abstain |

数量与例外的候选约束核对了[《促进和规范数据跨境流动规定》](https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm)、[《数据出境安全评估办法》](https://www.cac.gov.cn/2022-07/07/c_1658811536396503.htm)和[《个人信息出境标准合同办法》](https://www.cac.gov.cn/2023-02/24/c_1678884830036813.htm)的官方来源及本地条款。具体业务违法性和地方清单适用结论不由这一步自动审定。

## 第二轮候选

第二轮根据Fact Provenance完成后的真实首轮运行选择，不修改首轮题目和评分边界。

| 案例 | 新增原因 |
| --- | --- |
| `agent_aligned_intake_001` | 材料、intake、统计期间、去重人数、目的地与地域一致，排除输入歧义后观察重复追问 |
| `agent_historical_dedup_001` | 明示历史统计期与去重口径，区分题目遗漏与历史规则收口问题 |
| `agent_confirmed_material_conflict_001` | 已确认填报与材料中的CIIO身份相反，不能按来源标签自动选边 |
| `agent_requester_resume_001` | 用户明确要求先询问身份，观察实际暂停恢复及未核实回答的可信状态 |
| `agent_reviewer_instruction_001` | 对照即时答复：Reviewer操作指引不能成为身份事实或进入事实账本 |
| `agent_hr_exception_missing_001` | 只缺少一个会改变HR免予成立与否的要件，不默认例外成立或不成立 |
| `agent_important_data_001` | 已认定重要数据不能因零个人信息人数而免予安全评估 |
| `agent_small_sensitive_001` | 小量敏感信息不能套用普通信息不满10万人的免予条件 |
| `agent_freshness_holdout_001` | 汽车行业新官方材料尚未受控核验时，是否保留核心判断并触发freshness hold |

暂停恢复题的交互要求属于用户任务目标，不规定搜索、阅读和法律推理顺序。未产生实际暂停恢复时，只能称法律交付已评估，不能称暂停恢复能力已覆盖。

freshness题使用[工信部2026版汽车数据出境指引通知](https://www.miit.gov.cn/jgsj/waj/wjfb/art/2026/art_4d98b542f9ba44d3b4ecf50cd034e14b.html)的官方发布信息。评测专用语料完整移除该来源；真实服务索引保持不变，但生产工具会过滤所有不在本案语料中的命中，读取也不能访问留出来源。仅本案官方Web搜索返回固定发布摘要，正式引用、语义校验和生产模型仍按原逻辑运行。它是受控边界测试，不评价线上Web召回。固定返回无论查询措辞如何都相同，也不能用它证明查询构造质量。`controlled_chunks.jsonl`保存在本案目录，禁止多线程运行该fixture。

## 运行与失败复核

真实运行先确认 `python -m law_agent.review service-doctor` 可用。候选集仍可通过标准 `agent-eval --suite core` 入口运行；本次用专门脚本增加逐案落盘，防止整批结束前丢失证据。

```powershell
python scripts/run_agent_baseline.py --round 1 --workers 1 --output-dir data/review_runs/agent_core_new_round1
python scripts/run_agent_baseline.py --round 2 --workers 1 --output-dir data/review_runs/agent_core_new_round2
```

运行目录保存：`manifest.json`（模型、配置、基线提交、生产代码和语料散列、完整案例）、`status.json`（已完成结果）、每个案例的 `case.json`、`events.jsonl`、`state.json`、`payload.json`、`result.json`，最后生成 `summary.json`。事件和状态即时写盘。模型 judge 是独立的评测调用，默认仍为受测模型，报告必须披露同模型偏差；judge 故障不算通过。

后续运行还保存 `judge_input.json`，只向judge提供最终交付、事实账本及实际引用依据，不把生产语义校验的判词作为judge输入。运行记录散列覆盖生产Python模块，并记录运行期间是否发生变化。遇到确定的认证、余额或限流阻断，停止启动尚未运行的案例。用 `--case` 选取未完成案例、以新目录补跑，不覆盖原记录。暂停后缺少预设回答记为UNEVALUATED，不能据此宣称法律判断失败；追问是否多余单独结合原文复核。较早实跑状态见[历史基线报告](agent-baseline-20261008.md)。

失败分类允许多选：法律推理、遗漏例外、事实 grounding、法源覆盖、引用、澄清、abstention、freshness、预算耗尽、运行/评测基础设施。缺少法源只证明覆盖不足，不能直接归因为检索失败；要结合 checkpoint 中已检索/已阅读的证据区分“没找到”“找到了没采用”“采用了但理解错”。若 rubric 不明确、题目矛盾或 judge 错判，应记录为评测问题，不通过修改生产 Agent 迎合题目。

第二轮题目根据首轮真实失败选择，保留首轮输入、rubric 与结果，不能悄悄改题后覆盖旧分数。新增 freshness/new-source 题必须保证新官方材料真的未进入其受控证据环境；若使用可重复的法源留出或 Web fixture，须单列为受控边界测试，不能称为自主发现新法源的线上表现。

## 验收

本轮交付以案例、实跑证据与 bad-case 报告为主，不预设通过率目标。报告逐案列出判断边界、最终交付、证据、失败类别与可能原因，并标明 rubric/judge/运行设施的不确定性。最终人工审定后，才形成“当前 Production Agent 在一组人工审定的高风险案件上，哪里可靠、哪里不可靠、为什么”的能力基线。

Fact Provenance完成后的17案基线、授权的通用判断修复及相同案例复测见[当前基线说明](agent-baseline-post-provenance-20261009.md)。16个候选PASS中包含预算耗尽的系统abstain；HR题存在judge与候选约束分歧，不能将这个计数称为法律准确率。
