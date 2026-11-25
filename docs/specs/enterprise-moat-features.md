# 需求文档：主体累计台账、法规变更影响分析、审查纠错回流评测

> 读者：负责实施的编码 Agent（Codex）与代码审查人。
> 状态：待实施。三个功能相互独立，按 F1 → F2 → F3 顺序各自成一个 PR。
> 先读：`docs/architecture.md`、`docs/evaluation.md`、`docs/development.md`。本文中的文件路径与函数名均指当前 `main` 分支。

## 0. 背景与目标

CrossComply 的单案审查链路（冻结输入 → Agent → 引用门禁 → 语义校验 → 飞书审批）已经完成。本次三个功能补的是**通用对话式 Agent 结构上做不到**的能力：

| 功能 | 通用 Agent 为什么做不到 | 本项目已有的基础 |
| --- | --- | --- |
| F1 主体级年度累计台账 | 只看得到当前这一个案件，看不到同一主体的其他出境业务 | `transfer_activities`、已审批案件的冻结 intake |
| F2 法规变更影响分析 | 没有"上次结论引用了哪些条款"的记忆，法规变了也不会回头检查 | 引用精确到 `chunk_id`、知识库版本切换、`temporal.py` 时效规则 |
| F3 审查纠错回流评测 | 没有企业内的人工复核信号，无法形成回归集 | `case_feedback`（含 `citation_verdicts`）、Agent 评测框架与审定包 |

**三个功能都不调用 LLM**，全部是确定性计算加人工确认。这是有意为之的设计，不要加入"让模型判断是否受影响"之类的逻辑。

## 1. 全局约束（三个功能都必须遵守）

1. **存储双实现**：每个新 store 都要同时提供 `Postgres*` 和 `InMemory*` 两个实现，接口一致（参照 `transfer_store.py`）。测试使用 InMemory 实现；涉及 SQL 的部分另加 `tests/test_*_postgres.py`，沿用现有 Postgres 测试的跳过条件。
2. **迁移**：新增 `alembic/versions/0021_*.py`、`0022_*.py`、`0023_*.py`，`down_revision` 依次衔接 `0020_transfer_work_inbox`。使用原生 SQL（`op.execute`）风格，与 0020 一致；必须写 `downgrade`。
3. **Agent 边界不变**：Agent 不能写台账、不能确认影响、不能生成评测用例。对外动作（发起复审、忽略影响、导出用例）都必须由 reviewer 或 admin 通过 HTTP 发起，并写入 `case_events`。
4. **冻结原则**：凡是进入 Agent 上下文的数据，都必须在排队时冻结为快照，任务 resume 时不能重新计算（参照 `IntakeSnapshot`）。
5. **权限**：`requester` 只能看到自己作为 owner 的业务、主体和案件；所有写操作只允许 `reviewer` 和 `admin`，F1 中登记本人业务除外（沿用现有规则）。
6. **HTTP 路由**：新路由放在 `law_agent/review/http/` 下的新模块中，在 `api.py` 里组装，风格参照 `http/transfers.py`。路由函数用 `def`（不用 `async def`），避免阻塞事件循环。
7. **前端**：React + TypeScript，类型写在 `frontend/src/types/`，接口写在 `frontend/src/api/client.ts`。样式复用 `styles/tokens.css` 中的 token，不新增颜色常量。新增的纯函数要配 vitest 测试。
8. **测试与 CI**：`pytest`、`cd frontend && npm test && npm run build` 必须全部通过。新测试不得调用付费模型。
9. **文档**：每个功能合入时同步更新 `docs/architecture.md`（运行组件、程序保证、代码地图）和本文末尾的"实施记录"。F3 还要更新 `docs/evaluation.md`。
10. **措辞**：UI 文案和代码注释使用中文，与现有代码保持一致。不得声称系统"判定合规"，只能说"提示 / 待确认"。

---

## F1 主体级年度累计台账

### F1.1 问题

《促进和规范数据跨境流动规定》中的人数门槛（非敏感个人信息 10 万 / 100 万，敏感个人信息 1 万）按**数据处理者自当年 1 月 1 日起累计**计算。现在每个案件只看本案人数（`IntakePayload.annual_non_sensitive_count`），前端 `transferThresholds.ts` 也只展示本案。

典型漏判：同一法人有 A 业务 6 万人、B 业务 5 万人，每笔单看都在"免予"区间，合计 11 万人已经进入"标准合同或认证"区间。

### F1.2 范围

- 做：主体标识；按"主体 + 自然年"汇总已审批业务的人数；排队审查时把汇总结果冻结并注入 Agent；跨档时生成待办；在前端展示。
- 不做：跨业务的自然人去重（系统没有自然人明细，**不得**伪造去重结果）；判断某个免予情形下的出境是否计入累计（由审查人在业务登记时标记，系统不做法律判断）；自动修改任何案件的结论。

### F1.3 主体标识

1. 在 `law_agent/review/http/schemas.py::IntakePayload` 新增 `business_entity: str = ""`，含义为"申报主体（数据处理者法人全称）"。
   - 前端申报表单（`WorkbenchPage` / `FactValueInput` 所在的填报流程，以及 `factFields.ts`）增加这个字段，标签为"申报主体（法人全称）"，非必填。
   - `transfers.py::SCOPE_FIELDS` **不要**加入该字段：业务主体的变化已由 `scope_diff` 单独比较 `business_entity`。
   - `ReviewFacts`、`LLMReviewFacts` **不要**加入该字段，事实抽取不负责主体识别。
2. 新增 `law_agent/review/portfolio.py`，提供 `entity_key(name: str) -> str`：去掉首尾空白，全角字符转半角，`casefold()`，去掉所有空白和括号的全半角差异（"（" 与 "(" 视为相同）。返回空串表示无法归属。
3. 主体归属优先级：
   - 已登记为持续业务的案件：取 `transfer_activity_reviews.business_entity`；若案件是业务的 origin 案件，则取 `transfer_activities.business_entity`。
   - 其他案件：取冻结 intake 中的 `business_entity`。
   - 都为空：该案件不参与汇总。前端在案件详情页提示"未填写申报主体，无法与同主体其他业务合并计算"。

### F1.4 汇总口径（`portfolio.py`）

```python
@dataclass(frozen=True)
class PortfolioContribution:
    activity_id: str
    activity_title: str
    baseline_case_id: str          # latest_approved_case(...) 的结果
    intake_snapshot_id: str
    snapshot_year: int             # 冻结 intake 的 created_at 按 business_today 时区换算的年份
    non_sensitive: int | None      # 解析失败或为空时为 None
    sensitive: int | None
    count_period: str
    counts_toward_total: bool
    status: Literal["counted", "stale_year", "unparsed", "excluded"]

@dataclass(frozen=True)
class PortfolioSummary:
    entity_key: str
    entity_name: str               # 最近一次使用的原始名称
    year: int
    ciio_any: bool                 # 任一贡献业务的冻结 intake 中 ciio_status == "ciio"
    contributions: list[PortfolioContribution]
    non_sensitive_sum: int         # 仅统计 counted
    non_sensitive_max: int
    sensitive_sum: int
    sensitive_max: int
    band_upper: Band               # 按 sum 计算
    band_lower: Band               # 按 max 计算
    fingerprint: str               # 对规范化 payload 做 sha256
```

规则：

1. 数据来源只有**已登记的持续出境业务**（`transfer_activities`），每个业务取它的当前基线：`transfer_store.latest_approved_case` → 最新审批对应的 `task.intake_snapshot_id` → 冻结 intake。复用 `http/transfers.py::approved_context` 的校验逻辑，把它抽成 `portfolio.py` 或 `transfer_store.py` 中的公共函数，两处共用，不要复制。
2. 人数解析：`int(value.replace(",", "").replace("，", "").strip())`，失败则为 `None`，状态记为 `unparsed`。不要识别"约 12 万"之类的中文数量，保持保守。
3. `count_period` 不是 `current_year_cumulative` 或 `annual_estimate` 时，状态记为 `unparsed`。
4. `snapshot_year != year` 时，状态记为 `stale_year`，不计入合计。UI 提示"需更新本年度人数"。
5. `counts_toward_total == False` 时，状态记为 `excluded`。
6. 档位 `Band = Literal["exempt", "contract_or_cert", "assessment"]`，门槛常量放在 `portfolio.py` 顶部，并注释法源（第五、七、八条），数值与 `frontend/src/utils/transferThresholds.ts` 保持一致：
   - 非敏感 `< 100_000` 且敏感 `== 0` → `exempt`
   - 非敏感 `>= 1_000_000` 或敏感 `>= 10_000` → `assessment`
   - 其余 → `contract_or_cert`
7. `ciio_any == True` 时仍然计算，但 UI 与 Agent 注入中必须标注"存在 CIIO 身份记录，人数门槛可能不适用"。
8. **上下界语义**（贯穿 UI 与 Agent 注入）：
   - `sum` 是简单加总，未跨业务去重，是**上限**；
   - `max` 是单一业务最大值，是**下限**；
   - `band_lower == band_upper`：可以确定处于该档；
   - 两者不同：表述为"去重后可能处于 X 至 Y 档，需确认跨业务去重人数"。

### F1.5 数据模型（迁移 `0021_entity_portfolio`）

```sql
ALTER TABLE transfer_activities
  ADD COLUMN entity_key text NOT NULL DEFAULT '',
  ADD COLUMN counts_toward_total boolean NOT NULL DEFAULT true,
  ADD COLUMN exclusion_note text NOT NULL DEFAULT '';
-- 迁移内用 SQL 回填 entity_key：lower(regexp_replace(business_entity, '\s', '', 'g'))
-- 回填与 Python 函数可能存在细微差异：提供 CLI `python -m law_agent.review backfill-entity-keys` 用 Python 函数重算，部署文档写明执行顺序。
ALTER TABLE transfer_activity_reviews ADD COLUMN entity_key text NOT NULL DEFAULT '';
CREATE INDEX transfer_activities_entity_idx ON transfer_activities (entity_key);

CREATE TABLE portfolio_snapshots (
  id text PRIMARY KEY,                 -- "portfolio_snapshot_<hex>"
  case_id text NOT NULL REFERENCES review_cases(id),
  entity_key text NOT NULL,
  year integer NOT NULL,
  excluded_activity_id text,           -- 本案自身所属业务，不计入"其他业务"
  payload jsonb NOT NULL,              -- PortfolioSummary 序列化
  fingerprint text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE review_tasks ADD COLUMN portfolio_snapshot_id text REFERENCES portfolio_snapshots(id);

CREATE TABLE portfolio_alert_acks (
  entity_key text NOT NULL,
  year integer NOT NULL,
  band text NOT NULL,
  acked_by text NOT NULL REFERENCES users(id),
  note text NOT NULL,
  acked_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_key, year, band)
);
```

- `transfer_store.register` 与 `start_review` 写入时同步计算 `entity_key`。
- `RegisterTransfer` 新增可选字段 `counts_toward_total: bool = True` 与 `exclusion_note: str = ""`；`counts_toward_total=False` 时 `exclusion_note` 必填（422）。只有 reviewer 和 admin 可以设为 `False`（requester 设置时返回 403）。
- `ReviewTask` dataclass 新增 `portfolio_snapshot_id: str | None = None`，两个 store 的读写都要覆盖。`enqueue_review_task` 新增可选参数 `portfolio_snapshot_id`，并把它纳入 idempotency key 的散列内容（参照现有 `intake_snapshot_id` 的处理）。

### F1.6 冻结与注入

1. 新增 `portfolio.freeze_portfolio(case, *, transfers, cases, enterprise, governance, today) -> PortfolioSnapshot | None`：
   - 按 F1.3 确定本案的 `entity_key`，为空时返回 `None`；
   - 计算 `year = today.year` 的汇总，**排除本案所属业务**（`transfers.for_case(case_id)`）：本案是该业务的新版本，不能和旧基线重复计算；
   - 主体下没有其他业务时也要冻结（`contributions=[]`），以便留痕；
   - 写入 `portfolio_snapshots`，相同 `case_id + fingerprint` 复用已有记录。
2. 在**所有**排队审查的入口调用它，并把 id 传给 `enqueue_review_task`：
   - `law_agent/review/fact_confirmation.py::queue_review`
   - `law_agent/review/http/cases.py` 中调用 `queue_review` 的两处（约第 399 行、第 665 行附近）
   - `law_agent/review/evalset/agent_runner.py`：评测时由 `AgentCase.portfolio`（见 F1.9）构造，不查库
3. `law_agent/review/agent_runtime.py::execute_agent_task`：
   - 读取 `task.portfolio_snapshot_id` 对应的快照（`AgentRuntimeStore` 协议新增 `get_portfolio_snapshot`）；
   - 新任务时，把派生事实写入 `fact_ledger`（resume 时沿用 checkpoint，不重复写入）：
     - `FactLedgerEntry.source_type` 的 Literal 新增 `"transfer_register"`；`status` 的 Literal 新增 `"derived"`，并在 `description` 中补充："derived 为系统按已审批业务台账计算的派生值，不是申请人陈述，也未跨业务去重"；
     - 写入的字段：`entity_other_activities_count`、`entity_other_non_sensitive_sum`、`entity_other_non_sensitive_max`、`entity_other_sensitive_sum`、`entity_other_sensitive_max`、`entity_stale_or_unparsed_activities`（列出标题），`source_ref = portfolio_snapshot.id`；
   - 把 `intake={"id":..., "facts":...}` 扩展为同时携带 `"portfolio": payload`，并同样传给 `finalize`、`abstain` 的 `intake_snapshot` 参数。
4. `law_agent/review/agent.py` 系统提示（第 225 行附近，"区分冻结填报、材料原文……"那一段）追加一句："transfer_register/derived 是同一主体其他已审批出境业务的人数合计与最大值，未跨业务去重，只能作为累计门槛的上下界；本案人数需与其合并判断时须说明这一口径，不得把合计当作已去重人数。"不要改动其他提示词。
5. `semantic_grounding.py`：`fact_ledger` 已经传入，确认 `source_ref` 白名单（第 186 行附近）和 `agent.py` 第 720 行附近的 `revision_targets` 白名单能接受 portfolio snapshot id（这两处已基于 `fact_ledger` 构造，预计无需改动，补一条测试确认即可）。

### F1.7 跨档提醒（WorkInbox）

1. `WorkInbox.__init__` 新增依赖 `portfolio`（一个只读服务对象，提供 `summaries(user, year)` 与 `acked(entity_key, year, band)`）。`work_reminders.py` 构造 `WorkInbox` 的地方同步更新。
2. `WorkInbox.list` 对 reviewer 和 admin 增加：
   - 对每个主体计算**包含全部业务**的当年汇总；
   - 若 `band_upper` 高于该主体所有 counted 业务基线中各自的最高单案档位（即"合起来比每一笔单看都高"），并且 `(entity_key, year, band_upper)` 未确认，则生成 `kind="portfolio_threshold"` 的 team 项：
     - 标题 `"{entity_name} 当年累计可能跨档"`，原因 `"同主体 {n} 项业务合计 {sum} 人（未去重），单项最高 {max} 人"`；
     - 动作 `"核对主体累计"`，链接 `/cases/business?entity={entity_key}`；
     - `key=f"{entity_key}:{year}:{band_upper}"`。
3. 新路由 `POST /api/entities/{entity_key}/portfolio/ack`，请求体为 `{year, band, note}`（`note` 必填，最长 1600 字）。仅 reviewer 和 admin 可调用。写入 `portfolio_alert_acks`，并为该主体下每个业务的当前基线案件写一条 `case_events`（`event_type="portfolio_threshold_acknowledged"`）。

### F1.8 查询 API 与前端

API（新模块 `law_agent/review/http/portfolio.py`）：

- `GET /api/entities/portfolio?year=2026` → `{"items": [PortfolioSummary...]}`。requester 只能看到自己作为 owner 的业务所属的主体，且 `contributions` 中只包含自己的业务（其他业务只给出计数，不给标题）。
- `GET /api/entities/{entity_key}/portfolio?year=` → 单个主体的汇总，权限同上。
- `GET /api/cases/{case_id}/portfolio-snapshot` → 当前最新任务所冻结的快照，没有则返回 404。

前端：

1. `TransferActivitiesPage.tsx` 顶部增加"按主体查看"切换：每个主体一行，复用 `transferThresholds.ts` 的刻度条，同时画出 `max`（实心标记，标注"单项最高"）与 `sum`（空心标记，标注"合计·未去重"）两个位置；展开后列出各业务的贡献及其状态标签（已计入 / 非本年度 / 人数无法解析 / 已排除，并显示排除说明）。支持 `?entity=` 深链定位。
2. `transferThresholds.ts` 新增 `buildRangeScale(lower, upper, sensitiveLower, sensitiveUpper)`，配 vitest 测试，覆盖同档、跨档、`ciio_any` 三种情况。
3. 案件详情页（`ReportFigures.tsx` 或 `CaseFactsPanel.tsx` 附近）增加"同主体累计"卡片，展示该任务冻结的快照，并注明"审查时冻结，之后的业务变化不影响本报告"。
4. 业务登记弹窗增加"计入主体累计"开关与排除说明，仅 reviewer 可见。

### F1.9 评测接入

- `AgentCase` 新增 `portfolio: dict | None = None`，形状与 `PortfolioSummary` 的序列化一致；`agent_runner.py` 在构造任务时据此直接生成 `portfolio_snapshot`。
- 在新文件 `agent_cases_portfolio.py` 新增 3 个 candidate 用例，作为独立的 `suite="portfolio"`，在 `agent_cases.get_agent_cases` 和 `cli.py` 的 `agent-eval --suite` choices 中注册：
  1. 本案 6 万，同主体其他业务合计 5 万、最大 5 万：合计跨入"标准合同或认证"，下限仍是"免予"。允许的判断："说明去重后可能跨档，要求确认跨业务去重人数"。禁止的判断："仅凭本案 6 万直接认定免予"，以及"把 11 万当作已去重人数直接认定必须签标准合同"。
  2. 本案 3 万，其他业务最大 98 万：单项已经接近 100 万档，检验 Agent 是否把下限同样纳入考虑。
  3. 其他业务存在 `stale_year`：检验 Agent 是否指出数据过期，而不是把它当作 0。
- `docs/evaluation.md` 增加 `portfolio` 集合说明。目标指标："跨业务累计漏判率"——有台账注入与无台账注入（把 `portfolio` 置空、同一用例）两组对比。

### F1.10 验收标准

- [ ] 同主体两项已审批业务（6 万 + 5 万）登记后，reviewer 的待办中出现 `portfolio_threshold`；确认后该项消失，并写入 `case_events`。
- [ ] 对其中一项业务发起变更复审时，冻结快照只包含另一项业务（5 万），且 Agent 的 `fact_ledger` 中出现 `transfer_register/derived` 条目。
- [ ] 任务 resume 后，即使期间新登记了同主体业务，快照也保持不变。
- [ ] requester 调用主体 API 时看不到他人业务的标题。
- [ ] 主体名称为"上海某某（中国）有限公司"与"上海某某(中国)有限公司 "时归为同一主体。
- [ ] 人数为"约12万"时状态为 `unparsed`，不计入合计。

### F1.11 测试清单

`tests/test_portfolio.py`（entity_key 规范化、解析、档位、上下界、stale/excluded、排除本案业务、fingerprint 稳定）、`tests/test_portfolio_api.py`（权限、ack、深链）、`tests/test_work_inbox_transfers.py`（追加跨档提醒用例）、`tests/test_agent_context.py` 或 `tests/test_review_agent.py`（ledger 注入与 resume 不重复写入）、`tests/test_enterprise_store.py`（`portfolio_snapshot_id` 读写与 idempotency）、前端 `transferThresholds.test.ts`。

---

## F2 法规变更影响分析（持续合规）

### F2.1 问题

案件审批后，所依据的法规可能被修订、废止或被新版本取代。现在的 `temporal.py` 只保证**新审查**使用审查时点有效的版本；已经通过的案件和持续出境业务不会收到任何提示。审查报告的引用已精确到 `chunk_id`（`ReviewResult.citations`），具备反查的条件。

### F2.2 范围

- 做：建立"案件 → 引用 chunk"索引；知识库每次变更后计算差异；反查受影响的案件，生成影响标记；接入待办、飞书提醒和审批前置检查；reviewer 可以忽略或发起复审。
- 不做：自动重跑审查；让 LLM 判断变更是否实质影响结论；修改已审批案件的状态。

### F2.3 数据模型（迁移 `0022_regulation_impact`）

```sql
CREATE TABLE case_citation_index (
  task_id text NOT NULL REFERENCES review_tasks(id),
  case_id text NOT NULL REFERENCES review_cases(id),
  chunk_id text NOT NULL,
  source_id text NOT NULL,
  instrument_key text NOT NULL DEFAULT '',   -- chunk.instrument_key 或 title.strip()，与 temporal.py 规则一致
  article_no text,
  title text NOT NULL,
  effective_date text,
  can_cite_clause boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, chunk_id)
);
CREATE INDEX case_citation_index_source_idx ON case_citation_index (source_id);
CREATE INDEX case_citation_index_instrument_idx ON case_citation_index (instrument_key);

CREATE TABLE knowledge_change_events (
  id text PRIMARY KEY,                       -- "kchange_<hex>"
  job_id text,                               -- 知识库任务 id；CLI 触发时为空
  source_id text NOT NULL,
  instrument_key text NOT NULL DEFAULT '',
  change_type text NOT NULL,                 -- added | body_changed | metadata_changed | removed | restored
  detail jsonb NOT NULL,                     -- 见 F2.4
  impact_status text NOT NULL DEFAULT 'pending',  -- pending | computed | failed
  impact_error text,
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE case_impact_flags (
  id text PRIMARY KEY,                       -- "impact_<hex>"
  change_event_id text NOT NULL REFERENCES knowledge_change_events(id),
  case_id text NOT NULL REFERENCES review_cases(id),
  task_id text NOT NULL REFERENCES review_tasks(id),
  activity_id text REFERENCES transfer_activities(id),
  reason text NOT NULL,                      -- 见 F2.5
  severity text NOT NULL,                    -- blocking | upcoming | informational
  chunk_ids jsonb NOT NULL,
  effective_on date,                         -- upcoming 时为新版本生效日
  status text NOT NULL DEFAULT 'open',       -- open | dismissed | rereview_started | resolved
  resolution_note text,
  resolved_by text REFERENCES users(id),
  resolved_at timestamptz,
  rereview_case_id text REFERENCES review_cases(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (change_event_id, case_id)
);
CREATE INDEX case_impact_flags_open_idx ON case_impact_flags (status, case_id);
```

新 store：`law_agent/review/impact_store.py`（`PostgresImpactStore` / `InMemoryImpactStore`），负责上面三张表。

### F2.4 引用索引与变更差异

**引用索引写入**

- `law_agent/review/worker.py` 中，任务以 succeeded 完成（`self._queue.complete_task(...)` 成功返回）之后，从 `result` 中取出 `review_result.citations`（先确认 result 的实际嵌套路径，见 `result_builder.py`），对每条引用 upsert 一行。写入失败要记日志但**不能**让任务失败，因为任务已经完成。同时提供补写入口。
- CLI：`python -m law_agent.review backfill-citation-index [--dry-run]`，遍历所有 succeeded 任务回填，可重复执行。
- `instrument_key` 从当前 `chunks.jsonl` 按 `chunk_id` 查找；找不到（chunk 已被删除）时用 `citation.title.strip()`。

**变更差异**（新模块 `law_agent/kb/change_detection.py`，纯函数，便于测试）

```python
def diff_source(
    before_source: SourceRecord | None, before_chunks: list[Chunk],
    after_source: SourceRecord | None, after_chunks: list[Chunk],
) -> SourceChange
```

`SourceChange.detail` 包含：

- `removed_chunk_ids`：在 before 中、不在 after 中的 chunk（`make_stable_chunks` 的 id 含正文散列，正文一变 id 就变）；
- `changed_articles`：按 `_chunk_locator` 相同、`chunk_id` 不同来配对，列出 `article_no` 及新旧 chunk id；
- `status_changes`：`law_status`、`effective_date`、`valid_to` 三个字段的前后值；
- `title_or_url_changed`：bool。

`change_type` 判定：`before_source is None` → `added`；`after_source is None` → `removed`；有 `removed_chunk_ids` → `body_changed`；否则只要 SourceRecord 有字段变化 → `metadata_changed`；完全一致时不产生事件。

**接入点**：`law_agent/review/http/knowledge.py::execute_knowledge_job`。对 import、metadata、delete、restore 四类任务，每个条目在调用 service 之前，先读出该 `source_id` 的旧 SourceRecord 和旧 chunks（`KnowledgeBaseAdminService` 新增只读方法 `source_snapshot(source_id) -> tuple[SourceRecord | None, list[Chunk]]`）；条目成功后再读新的一份，调用 `diff_source`，写入 `knowledge_change_events`，随后立即调用 F2.5 的影响计算。影响计算抛出异常时：把事件标记为 `impact_status=failed` 并记录 `impact_error`，**知识库任务本身仍算成功**；在条目结果里附上 `impact: {"status": "failed"}`。成功时附上 `impact: {"flagged_cases": n}`。

同时提供 `POST /api/knowledge/change-events/{id}/recompute`（仅 admin），用于重算失败的事件。

### F2.5 影响匹配规则（`law_agent/review/impact.py`）

**受检范围**：只检查"仍然有效"的结论，即：

- 状态为 `approved` / `conditionally_approved` 的案件，取其**最新审批对应的 task**（`governance.get_latest_approval(case_id).task_id`）；
- 状态为 `pending_feishu_approval` 的案件，取最新 succeeded 任务。

被更新的复审取代的旧基线不检查：若一个持续业务的 `latest_approved_case` 不是该案件，则跳过。

对每个变更事件，按以下顺序判定 `reason`。同一案件只取最严重的一条：

| reason | 条件 | severity |
| --- | --- | --- |
| `cited_text_changed` | 案件引用的 chunk 在 `removed_chunk_ids` 中，且该来源仍存在 | blocking |
| `source_removed` | `change_type=removed`，且案件引用了该来源的任意 chunk | blocking |
| `source_repealed` | `status_changes.law_status` 变为 `repealed` 或 `amended`，或 `valid_to` 被设为不晚于今天的日期 | blocking |
| `superseded` | `added`/`body_changed`/`metadata_changed` 后，同一 `instrument_key` 下存在其他来源的 `effective_date` 晚于案件所引来源的 `effective_date` 且不晚于今天（规则与 `temporal.filter_hits_as_of` 一致，抽成共用函数，不要复制） | blocking |
| `superseded_upcoming` | 同上，但新版本的 `effective_date` 晚于今天 | upcoming（写入 `effective_on`） |
| `metadata_only` | 只有标题或 URL 变化 | **不生成标记** |

- `restored`：按 `added` 处理（可能重新取代其他版本）。
- 同一 `(change_event_id, case_id)` 唯一，重复计算时跳过。
- 每生成一条标记，写一条 `case_events`（`event_type="regulation_impact_flagged"`，payload 含 `flag_id`、`reason`、`source_id`、`article_nos`）。
- 新增 CLI：`python -m law_agent.review impact-scan --as-of 2026-12-01`，按日期重新扫描 `superseded_upcoming`：生效日已到的标记升级为 `superseded / blocking`，并写入事件。由 `knowledge-worker` 每天调用一次，在 `law_agent/kb/enrichment_worker.py` 的主循环中按日期去重执行。

### F2.6 处置流程

新模块 `law_agent/review/http/impact.py`：

- `GET /api/impact-flags?status=open` → 列表，包含案件标题、业务、法规标题、条号、reason、severity、创建时间。reviewer 和 admin 可见全部；requester 只能看到自己作为 owner 的案件，且只读。
- `GET /api/knowledge/change-events?limit=` → 变更事件及其标记数量（仅 admin 和 reviewer）。
- `POST /api/impact-flags/{id}/dismiss`，请求体 `{note}`，`note` 必填。状态改为 `dismissed`，写入 `case_events`。
- `POST /api/impact-flags/{id}/rereview`，请求体 `{change_note?}`：
  - 案件属于某个持续业务，且是 `latest_approved_case`：调用现有 `transfers.start_review`，`change_note` 默认为 `"法规变更：《{title}》{article_no} {reason 的中文说明}"`；
  - 案件不属于任何业务：返回 409，提示"请先登记为持续出境业务"，前端引导用户先调用 `POST /api/transfer-activities` 再重试（复用现有登记流程，不新增"复制案件"逻辑）；
  - 成功后标记状态为 `rereview_started`，写入 `rereview_case_id`。
- 复审案件被飞书审批为 approved 或 conditionally_approved 后，把关联标记置为 `resolved`。在 `workflow.py` 处理验签审批事件、写回终态的路径中加钩子；钩子失败只记日志，不能影响审批终态的写入。

**审批前置**：`approval_readiness.py` 的 `ApprovalReadinessCode` 新增 `"regulation_impact"`；`assess_approval_readiness` 新增参数 `open_blocking_impacts: int`。处于 `pending_feishu_approval` 且存在 open 的 blocking 标记时，返回 `ApprovalReadiness("regulation_impact", "引用的法规已变更，请先处理影响提示。")`。所有调用方都要传入该参数，`grep assess_approval_readiness` 找全。

**待办**：`WorkInbox.list` 为 reviewer 和 admin 生成 `kind="regulation_impact"` 的 team 项（每个 open 标记一条），`upcoming` 标记把 `due_on` 设为 `effective_on`，这样会自动进入现有的飞书提醒节奏（`work_reminders.py` 无需改动）。链接为 `/governance?tab=impact&flag={id}`。

### F2.7 前端

1. `GovernanceConsolePage.tsx` 新增"法规变更影响"标签页：
   - 上半部分是变更事件时间线（法规标题、变更类型、条号、受影响案件数）；
   - 下半部分是影响标记表格（可按 severity 和 status 筛选），每行提供"忽略（需填写说明）"和"发起复审"两个按钮，支持 `?flag=` 深链高亮。
2. `KnowledgeBasePage.tsx` 的任务结果中，若条目带有 `impact.flagged_cases > 0`，显示"影响 N 个已审批案件"并链接到上面的标签页；`impact.status=failed` 时显示失败提示与重算按钮（仅 admin）。
3. `CaseDetailPage.tsx` 顶部：存在 open 标记时显示横幅"本报告引用的《X》第 Y 条已于 {date} 变更"，blocking 用警示色，upcoming 用提示色。
4. `TransferActivitiesPage.tsx` 的业务列表：存在 open 标记的业务显示"法规变更待处理"徽标。
5. 引用列表（`CitationList.tsx`）中，被标记的 chunk 旁显示"已变更"小标签。

### F2.8 演示与评测

- 提供脚本 `scripts/demo_regulation_change.py`（只操作 InMemory store 和临时语料目录，**不触碰生产数据**）：先构造 N 个以 2022 年《数据出境安全评估办法》及其问答为依据的已审批案件，再导入 2024 年《促进和规范数据跨境流动规定》（同一 `instrument_key` 或相关来源，按实际语料决定；做不到同一 key 时，用 metadata 把旧问答的 `valid_to` 设为新规生效日），最后输出被标记案件数、reason 分布和耗时。
- 写进 `docs/evaluation.md` 的离线指标：在构造集上，影响标记的**召回率**（应标记的案件全部被标记）和**精确率**（只改标题或 URL 不产生标记），以及单次知识库任务的影响计算耗时（P50）。

### F2.9 验收标准

- [ ] 一个 approved 案件引用了 chunk C；把来源正文中 C 所在条文改动一字后重新导入 → 生成 `cited_text_changed / blocking` 标记，案件详情页出现横幅，reviewer 待办出现对应项。
- [ ] 只修改来源标题 → 产生 `metadata_changed` 事件，但不生成任何标记。
- [ ] 导入同一 `instrument_key`、生效日在未来的新版本 → 生成 `superseded_upcoming`；`impact-scan --as-of` 到生效日后升级为 `superseded / blocking`。
- [ ] `pending_feishu_approval` 案件存在 blocking 标记时，发起飞书审批被拒绝，返回 `regulation_impact`。
- [ ] 对持续业务的标记点击"发起复审"→ 生成变更复审案件；复审通过后标记变为 `resolved`。
- [ ] 影响计算抛出异常时，知识库任务仍为 succeeded，事件为 `impact_status=failed`，重算后恢复正常。
- [ ] 被更新复审取代的旧基线不会被标记。

### F2.10 测试清单

`tests/test_change_detection.py`（diff 的各分支）、`tests/test_regulation_impact.py`（匹配规则、只取最严重一条、唯一性、upcoming 升级、跳过旧基线）、`tests/test_impact_api.py`（权限、dismiss、rereview 两条分支、resolved 钩子）、`tests/test_kb.py`（知识库任务接入，含失败隔离）、`tests/test_review_worker.py`（引用索引写入及失败不影响任务）、`tests/test_case_workflow.py` 或审批相关测试（readiness 新代码）。

---

## F3 审查纠错回流 → 评测候选用例

### F3.1 问题

Agent 评测的法律判断用例将重建（上一轮的 25 个合成候选已移除），全部依赖人工构造。线上审查人已经在提交反馈（`case_feedback.conclusion_useful`、`missing_sources`、`citation_verdicts`），飞书审批也会产生驳回，但这些信号没有被利用起来。目标是把"审查人认为 Agent 错了"的案件，经脱敏和审定后，转为 Agent 回归评测用例，形成数据飞轮；同时给出一个可以对外展示的线上质量统计。

### F3.2 范围

- 做：候选信号收集；reviewer 显式发起"转为评测用例"；草稿编辑（脱敏 + rubric）；校验；导出为 `AgentCase` JSON；接入 `agent-eval` 的新套件；线上质量统计 API 与面板。
- 不做：未经人工操作自动生成用例；用 LLM 自动撰写 rubric 或自动脱敏；把导出的用例自动标为 `approved`（必须走现有审定流程）。

### F3.3 候选信号（只读，不落新表）

`law_agent/review/eval_feedback.py::candidate_signals(case_id)` 返回该案件的信号列表，供前端提示"值得转为用例"：

| kind | 来源 |
| --- | --- |
| `conclusion_not_useful` | 任一 reviewer/admin 的 `case_feedback.conclusion_useful == False` |
| `citation_wrong` | `citation_verdicts` 中有值为 `"wrong"` 的条目（附 citation ref 列表） |
| `missing_sources` | `missing_sources` 非空 |
| `approval_rejected` | 最新审批状态为 `rejected` |
| `rereview_changed` | （依赖 F2，可选）`rereview_case_id` 对应复审案件的 `risk_level` 或 `legal_path` 与原案件不同 |

### F3.4 数据模型（迁移 `0023_eval_case_drafts`）

```sql
CREATE TABLE eval_case_drafts (
  id text PRIMARY KEY,                       -- "evaldraft_<hex>"
  case_id text NOT NULL REFERENCES review_cases(id),
  task_id text NOT NULL REFERENCES review_tasks(id),
  signals jsonb NOT NULL,                    -- 创建时 candidate_signals 的快照
  status text NOT NULL DEFAULT 'draft',      -- draft | ready | exported | discarded
  question text NOT NULL,
  material_text text NOT NULL,               -- 预填冻结材料全文，由 reviewer 脱敏
  intake jsonb NOT NULL,                     -- 预填冻结 intake，由 reviewer 脱敏
  agent_output jsonb NOT NULL,               -- risk_level、legal_path、decision_summary、conclusion、引用的 source_id/chunk_id/article_no
  reviewer_correction text NOT NULL DEFAULT '',
  rubric jsonb NOT NULL DEFAULT '{}',        -- AgentRubric 序列化
  selection_reason text NOT NULL DEFAULT '',
  reference_basis jsonb NOT NULL DEFAULT '[]',
  deidentified boolean NOT NULL DEFAULT false,
  created_by text NOT NULL REFERENCES users(id),
  updated_by text NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  exported_case_id text,
  exported_sha256 text
);
CREATE UNIQUE INDEX eval_case_drafts_task_idx ON eval_case_drafts (task_id) WHERE status <> 'discarded';
```

新 store：`law_agent/review/eval_draft_store.py`（Postgres 与 InMemory 两个实现）。

### F3.5 流程与 API（新模块 `law_agent/review/http/eval_drafts.py`，全部仅限 reviewer 和 admin）

1. `GET /api/cases/{case_id}/eval-signals` → `candidate_signals` 的结果。
2. `POST /api/cases/{case_id}/eval-drafts` → 从案件**最新 succeeded 任务**创建草稿：
   - `question` 取 `case["question"]`；
   - `material_text` 为该任务材料快照中各版本 `parsed_text` 的拼接，格式与 runtime 传给 Agent 的 `material` 一致，找到并复用 worker 里的拼接函数；
   - `intake` 取冻结 intake 快照，并删除 `followup_answers`；
   - 同一任务已有非 discarded 草稿时返回 409，并给出已有草稿的 id；
   - 写入 `case_events`（`eval_draft_created`）。
3. `PATCH /api/eval-drafts/{id}` → 编辑 `question`、`material_text`、`intake`、`reviewer_correction`、`rubric`、`selection_reason`、`reference_basis`、`deidentified`。只有 `draft` 状态可以编辑。
4. `POST /api/eval-drafts/{id}/ready` → 执行 F3.6 的校验，通过后状态改为 `ready`，否则返回 422 和逐项错误列表。
5. `POST /api/eval-drafts/{id}/reopen`（ready → draft）、`POST /api/eval-drafts/{id}/discard`。
6. `GET /api/eval-drafts?status=` → 列表。

### F3.6 校验（`eval_feedback.validate_draft`）

1. 用草稿构造 `AgentCase`：
   - `case_id=f"field_{draft.id[-12:]}"`，`review_status="candidate"`；
   - `tags=["candidate", "field_feedback", *signal_kinds]`，`construction_round=1`，`source_case_id=None`；
   - 构造失败（pydantic 报错）时把错误转成可读的字段级提示。
2. 完整性要求：`selection_reason`、`reference_basis`、`rubric.allowed_judgments`、`rubric.forbidden_judgments` 均不能为空；`intake` 的键必须是 `IntakePayload` 字段，且能通过校验。这段检查写成 `law_agent/review/evalset/agent_schemas.py` 中的公共函数 `validate_candidate_case(case: AgentCase) -> list[str]`，供 F1 的 portfolio 用例测试与后续新建用例共用。
3. `deidentified` 必须为 `True`。
4. **脱敏残留扫描**（纯正则、确定性）：在 `question`、`material_text` 和 `intake` 的所有字符串值中检测：
   - 中国大陆手机号 `(?<!\d)1[3-9]\d{9}(?!\d)`
   - 18 位身份证号 `(?<!\d)\d{17}[\dXx](?!\d)`
   - 邮箱
   - 统一社会信用代码 `(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}(?![0-9A-Z])`
   - 草稿创建时案件的 `business_entity` 原文（若非空）

   任一命中即校验失败，并返回命中位置（字段名 + 偏移量 + 用 `*` 打码的片段）。**错误信息中不得回显完整原文。**
5. `rubric.must_cover_sources` 与 `forbidden_clause_sources` 中的 source_id 必须存在于当前语料 manifest 中。

### F3.7 导出与评测接入

1. CLI：`python -m law_agent.review.evalset.field_cases export --output law_agent/review/evalset/agent_cases_field.json [--dry-run]`
   - 读取所有 `ready` 草稿，按 `exported_case_id` 排序后写出 `AgentCase` 列表（`model_dump(mode="json")`，`ensure_ascii=False`，缩进 2）；
   - 文件已存在时**合并**：保留已有用例，同一 `case_id` 的内容若有变化则报错退出（防止静默覆盖已审定的用例）；
   - 成功后把草稿状态改为 `exported`，写入 `exported_case_id` 与 `exported_sha256`（`sha256(json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))`，放进 `agent_schemas.py` 的 `case_hash` 公共函数）；
   - 需要数据库连接，DSN 读取方式与其他 CLI 一致。
2. `agent_cases.py::get_agent_cases` 新增 `suite="field"`：文件存在时从 `agent_cases_field.json` 加载，否则返回空列表。`cli.py` 中 `agent-eval --suite` 的 choices 加入 `field`。
3. 审定：导出的用例一律保持 `review_status="candidate"`，程序不自动升级为 `approved`；人工审定流程随新一轮评测重建，本功能不负责。field 组与其他套件分开报告。
4. `docs/evaluation.md` 的案例集表格增加一行 `field`：来源为线上审查人纠错，经脱敏和审定后进入，与合成集分开统计。

### F3.8 线上质量统计

`GET /api/governance/quality?since=2026-01-01`（reviewer 和 admin 可见），只做确定性聚合：

```json
{
  "reviewed_cases": 0,                 // 有任一 reviewer/admin 反馈的案件数
  "conclusion_useful_rate": 0.0,       // useful=true / (true+false)，分母为 0 时为 null
  "citation_checks": {"correct": 0, "wrong": 0, "accuracy": null},
  "approval_outcomes": {"approved": 0, "conditionally_approved": 0, "rejected": 0},
  "eval_drafts": {"draft": 0, "ready": 0, "exported": 0, "discarded": 0},
  "regulation_impact": {"open": 0, "dismissed": 0, "rereview_started": 0, "resolved": 0}  // F2 未合入时省略该字段
}
```

前端在 `GovernanceConsolePage.tsx` 新增"质量与评测"标签页：顶部是上述指标卡（样本量为 0 时显示"暂无数据"，不显示 0%），下方是草稿列表和编辑表单：

- 左栏为脱敏编辑区（材料、问题、intake），右栏为只读的 Agent 原输出和审查人纠正说明；
- 下方是 rubric 编辑：`allowed_judgments`、`forbidden_judgments`、`must_cover_sources`（从引用中选择或手动输入）、`required_exceptions`、`must_not_assume`、`clarification_expectations`、`should_abstain`（三态）；
- "标记为已脱敏"复选框；"校验并标为就绪"按钮，校验失败时把错误逐项显示在对应字段旁。

`FeedbackPanel.tsx`：当 reviewer 提交了负面反馈或判定引用错误后，显示"转为评测用例"按钮（调用创建草稿接口，成功后跳转到编辑表单）。

### F3.9 验收标准

- [ ] reviewer 将某条引用判为 wrong 后，`eval-signals` 返回 `citation_wrong`，`FeedbackPanel` 出现"转为评测用例"按钮。
- [ ] 草稿中仍有手机号时，标为就绪返回 422，报错中带打码片段，不含原号码。
- [ ] rubric 不完整、`deidentified=false` 或 source_id 不存在时，均无法标为就绪。
- [ ] 导出后 `python -m law_agent.review agent-eval --suite field --no-judge` 能加载这些用例（本地运行，不纳入 CI）。
- [ ] 对已导出的同一 `case_id` 修改内容后再次导出，CLI 报错退出，原文件不变。
- [ ] requester 调用所有 F3 接口均返回 403 或 404。
- [ ] 质量统计在无数据时返回 null 而不是 0。

### F3.10 测试清单

`tests/test_eval_feedback.py`（信号、校验各分支、正则扫描与打码）、`tests/test_eval_drafts_api.py`（权限、状态机、唯一约束）、`tests/test_field_cases_export.py`（合并、冲突报错、dry-run、`case_hash` 一致）、`tests/test_agent_eval.py`（field 套件加载，文件缺失时为空）、`tests/test_governance_quality.py`（聚合与分母为 0）、前端编辑表单的校验错误映射纯函数测试。

---

## 4. 实施顺序与 PR 划分

| PR | 内容 | 依赖 |
| --- | --- | --- |
| 1 | F1：迁移 0021、`portfolio.py`、冻结与注入、待办、API、前端、评测集 | 无 |
| 2 | F2：迁移 0022、引用索引与回填、变更差异、影响匹配、处置 API、readiness、待办、前端、演示脚本 | 无（与 F1 只在 `WorkInbox` 构造函数上可能冲突，后合入者负责 rebase） |
| 3 | F3：迁移 0023、草稿 store、校验、导出、field 套件、质量统计、前端 | `rereview_changed` 信号与质量统计的 `regulation_impact` 字段依赖 F2，F2 未合入时跳过 |

每个 PR 的描述需包含：迁移说明（含回填 CLI 的执行顺序）、新增环境变量（预计没有）、验收清单逐项勾选结果、截图（前端改动）。

## 5. 明确不要做的事

- 不要让 Agent 新增工具来读取台账、影响标记或评测草稿。F1 通过冻结快照注入，F2 和 F3 与 Agent 无关。
- 不要在影响分析或评测校验里调用 LLM。
- 不要在 F1 中输出"去重后人数"，也不要把合计称为"累计人数"而不加"未去重"说明。
- 不要修改已审批案件的状态、报告或引用；影响标记是独立的记录。
- 不要恢复已移除的 `agent_review.py`、`agent_case_review.json` 及 core/holdout 用例。
- 不要把 `data/` 下的任何文件加入 Git。

## 6. 实施记录

（实施方逐 PR 填写：合入日期、偏离本文之处及原因、遗留问题。）
