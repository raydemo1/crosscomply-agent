# 数据治理设计

## 目标

CrossComply 的数据治理底座（`law_agent/data`）把多来源、多格式、多噪声的法律法规、政策材料、隐私政策语料加工成可检索、可评测、可追溯的知识库。它只产出带完整来源元数据、结构信息和引用资格的检索 Chunk，在线审查链路只消费这些 Chunk，不感知原始文件如何被清洗。

本文档描述当前已实现的流水线：从候选来源清单（Source Manifest）到可入库 `chunks.jsonl` 的完整链路，以及每个中间产物的 schema 与验收口径。

## 数据治理流程

```text
Source Manifest（候选来源 CSV）
  → fetch          按 include_in_mvp 下载原始文件到 data/raw/
  → normalize      解析为统一 Document 记录（documents.normalized.jsonl）
  → clean          确定性清洗（documents.cleaned.jsonl）
  → enrich         语义增强（documents.enriched.jsonl）
  → chunk          结构感知分块（chunks.jsonl）
  → evalset build  生成检索评测候选（retrieval_cases.jsonl）
  → report governance  生成数据治理报告
```

每个中间产物都保留为独立 JSONL，便于逐阶段比较、抽样审核和失败后重跑；索引可以随时从 `chunks.jsonl` 重建，不把数据库当作唯一真相源。

单条命令链：

```powershell
python -m law_agent.data manifest build --topic data_compliance --from-flk --limit 5
python -m law_agent.data manifest validate data/manifests/source_manifest.csv
python -m law_agent.data fetch
python -m law_agent.data normalize
python -m law_agent.data clean run
python -m law_agent.data enrich
python -m law_agent.data chunk
python -m law_agent.data evalset build
python -m law_agent.data report governance
```

配置好 manifest 和 LLM 后，也可以一条命令跑完：

```powershell
python -m law_agent.data pipeline run
```

## 目录结构

CLI 默认中间路径是 `data/manifests/`、`data/raw/`、`data/normalized/`、`data/cleaned/`、`data/enriched/`、`data/chunks/`、`data/eval/`。当前实际落地的语料包（2026-07-02 快照）在 `data/corpus/legal_docs_20260702/`：

```text
data/corpus/legal_docs_20260702/
  source_manifest.review.csv        # 人工确认后的来源清单
  raw/                              # 下载的原始材料
  documents.normalized.jsonl        # 统一 Document
  documents.cleaned.jsonl           # 清洗后文档
  documents.enriched.jsonl          # 语义增强后文档
  chunks.jsonl                      # 检索分块（含引用角色/引用资格）
  retrieval_cases.jsonl             # 检索评测候选集
  citation_policy.review.csv        # 引用角色逐来源确认表
  fetch_status.csv                  # 采集状态
  collection_summary.json           # 语料统计
  REVIEW_INDEX.md                   # 语料包说明
```

`data/` 目录默认被 git 忽略（`.gitignore` 中 `/data/`），只提交 schema、样例与报告，真实数据与本地索引不进 Git。

## 版本控制边界

不进 Git：

1. `data/raw/`、`data/normalized/`、`data/cleaned/`、`data/enriched/`、`data/chunks/`、`data/eval/`
2. `data/corpus/`（语料快照、模型缓存、评测运行产物）
3. 本地索引、缓存和下载文件

可以进 Git：

1. `data/manifests/*.schema.json`
2. 小规模示例 manifest（如 `data/manifests/source_manifest.example.csv`）
3. 数据治理报告与数据源说明文档
4. 评测集场景定义（在 `law_agent/review/evalset/` 内作为代码维护）

## 模块结构

`law_agent/data` 按流水线阶段拆分，模块边界清晰、可独立测试：

| 模块 | 职责 |
| --- | --- |
| `manifest.py` | 从 FLK 搜索接口生成候选来源清单；内置 `data_compliance` 主题的种子来源（个人信息保护法、数据安全法、网络安全法） |
| `fetchers/` | 原始材料采集：`flk_npc.py`（国家法律法规数据库搜索/下载签名解析）、`generic.py`（通用 URL 下载器） |
| `normalize.py` | 把原始文件解析为统一 `Document`，记录解析器和解析器版本；内置 parser router（见下） |
| `cleaners/` | 确定性清洗：`common.py`（通用规则与命中计数）、`pipeline.py`（`clean_document`） |
| `enrichment/` | `generator.py`：通过 OpenAI-compatible LLM 生成摘要、关键词、可回答问题、主题标签、风险标签等语义字段 |
| `chunking/` | 结构感知分块：`law.py`（法律法规按条/款拆分）、`structured.py`（问答、表格、指南按标题拆分）、`pipeline.py`（按文档类型路由 + tiny chunk 合并） |
| `evalset/` | `build_cases.py`：从 chunks 自动构建检索评测候选 |
| `reports/` | `governance_report.py`：数据治理统计报告 |
| `citation_policy.py` | 引用资格三层模型：法律效力（`has_legal_effect`）、条款定位（`has_clause_locator`）与 `can_cite_clause` / `can_cite_clause_chunk` 判定；案件适用性不在这里硬过滤 |
| `schemas.py` | `SourceRecord`、`Document`、`CleanedDocument`、`EnrichedDocument`、`Enrichment`、`Chunk` 等 Pydantic 模型 |
| `io.py` | JSONL / CSV manifest / JSON 的读写 |
| `cli.py` | argparse CLI（`python -m law_agent.data`） |

## Source Manifest

`source_manifest` 是入库前的人工确认清单，字段由 `SourceRecord` 定义，包括：

1. `source_id`、`title`、`source_url`、`download_url`、`source_site`
2. `doc_type`：`law`、`regulation`、`policy`、`faq`、`guideline`、`privacy_policy`、`internal_policy`、`case`、`contract`
3. `authority`：`national_law`、`administrative_regulation`、`ministry_policy`、`local_regulation`、`judicial_interpretation`、`public_interpretation`、`privacy_policy`、`simulated_internal_policy`、`unknown`
4. `law_status`：`effective`、`not_yet_effective`、`amended`、`repealed`、`unknown`
5. 法律元数据：`publish_date`、`effective_date`、`issuing_body`、`applicable_region`、`legal_domain`、`applicable_subjects`、`topic_tags`
6. 案例/合同字段（第一版法规数据可为空，后续案例与合同接入时启用）：`case_no`、`court`、`trial_instance`、`contract_parties`、`clause_type`
7. 门控字段：`include_in_mvp`（只有为 `true` 才进入 fetch）、`review_note`

来源纳入遵循“脚本负责发现，人负责确认”：`manifest build --from-flk` 通过 `flk.npc.gov.cn` 公开搜索接口生成候选，人工审核 `include_in_mvp` 与法律元数据后再进入流水线。

## 统一 Document 格式

所有来源材料先解析为统一 `Document`，再进入清洗、语义增强、分块。`Document` 继承 `SourceRecord` 的全部元数据字段，并增加：

1. `raw_format`：原始文件格式（如 `docx`、`pdf`、`html`）。
2. `text`：解析后的正文文本（表格以 HTML 片段保留，`|` 分隔单元格）。
3. `attachments`：解析过程中发现的附件。
4. `ingest_meta`：记录 `fetched_at`、`parser`、`parser_version`，便于追溯数据处理过程。

清洗后的 `CleanedDocument` 额外记录 `cleaning_version` 和 `cleaning_rule_hits`（每类规则的命中次数）；语义增强后的 `EnrichedDocument` 额外携带 `enrichment` 字段。

## 引用资格三层模型

引用资格拆成三个彼此独立的问题，由 `law_agent/data/citation_policy.py` 集中判定，在线审查的引用门禁共用同一策略源：

1. **法律效力**（`has_legal_effect`）——这份文件本身是不是法律规范。要求 `library_kind="legal"`，并按角色与文件类型判定：
   - `primary_legal_basis`：具有条款效力；
   - `conditional_local_basis` / `conditional_industry_basis`：角色只记录规范在**哪里/对谁**适用，不改变文件性质；当 `doc_type ∈ {law, regulation, judicial_interpretation}` 时仍具有条款效力；
   - `implementation_reference` / `interpretation_auxiliary`：标准、备案指南、模板、政策问答等不是法律规范，没有条款效力——可被检索、可在证据面板准确转述其内容与待核条件，但不能作为结论级法条引用；
   - `library_kind="internal_policy"` 的内部制度一律没有条款效力。
2. **条款定位**（`has_clause_locator`）——chunk 必须带具体 `article_no`（如“第三十九条”）。
   `can_cite_clause_chunk(source, article_no)` = 法律效力 ∧ 条款定位，两个硬条件缺一不可。
3. **案件适用性**（地域、行业、日期）——**不是引用门禁**。检索阶段由 `law_agent/review/retrieval/boosts.py` 做软加权（地域命中加权、明确错配轻降、解读类来源轻降，但一律不过滤），适用边界写入引用的 scope note；在线语义校验再核对已确认事实是否落在该法源的地域/行业/时点范围内（不满足判 unsupported，事实不足判 uncertain）。地方性、行业性规范因此既保留条款效力，其适用边界又始终可见。

各角色含义：

| 角色 | 含义 |
| --- | --- |
| `primary_legal_basis` | 正式依据层（个人信息保护法、数据安全法、出境评估办法、标准合同办法、跨境流动规定等） |
| `conditional_local_basis` | 地方性法规/规章：是否规范按 `doc_type` 判定，角色只标记地域适用边界 |
| `conditional_industry_basis` | 行业性条件规范：同上，角色只标记行业适用边界 |
| `implementation_reference` | 标准、备案指南、模板体系文件，用于落地操作说明 |
| `interpretation_auxiliary` | 答记者问、政策问答、理解与适用，用于解释口径和查询增强 |

每个 chunk 进入检索前都带上 `citation_role` 与按来源预算好的 `can_cite_clause`。

## 清洗规则

第一版清洗为确定性规则，不改写法规正文。`clean_text` 按顺序执行并记录每类命中数：

1. 移除控制字符、统一换行与空白、折叠重复空行。
2. 去除重复标题行。
3. 移除目录块：法规风格（“目录”标题 + 章节目录重复）与标准风格（点线目录 + 前言/引言边界）两种策略，只有检测到真实目录结构才删除，避免误删正文。
4. 移除机械噪声行：图片占位符、Markdown 表格分隔线、PDF 页数行、“可从以下网址获得”来源声明、独立来源 URL、独立年月行、PDF 封面碎片、点线目录行、网页 boilerplate（导航、备案号、打印分享按钮、CMS 信息等）。
5. 修复 PDF 字符间距伪影（如 `D a t a` → `Data`）。
6. 将孤立的条款编号行（如 `3.2`）合并进下一行标题，避免下游产生单字符 chunk。
7. 折叠多余空行。

清洗规则集中于 `cleaners/common.py`，命中计数写入 `CleanedDocument.cleaning_rule_hits`，供治理报告和抽样审核使用。

## 语义增强

`enrich_document` 通过 OpenAI-compatible LLM（DeepSeek）为每个清洗后文档生成语义字段，只生成元数据、不改写法规正文：

```json
{
  "summary": "一句话到三句话摘要",
  "keywords": ["个人信息", "数据出境", "单独同意"],
  "questions": ["什么情况下需要申报数据出境安全评估？"],
  "topic_tags": ["数据出境", "个人信息保护"],
  "applicable_subjects": ["个人信息处理者"],
  "risk_tags": ["高风险出境", "敏感个人信息"]
}
```

`authority_level` 与 `effective_status` 直接继承文档元数据，不由 LLM 生成；`enrichment_meta` 记录 `model`、`prompt_version`、`generated_at`。

## 分块策略

分块按文档类型路由（`chunking/pipeline.py`）：`doc_type` 为 `law`/`regulation`，或文本含 3 条及以上“第 X 条”结构时走法规分块，否则走结构化分块。两种策略都产出带完整来源元数据、`heading_path`、`citation_label`、`citation_role`、`can_cite_clause` 和前后邻居（`prev_chunk_id`/`next_chunk_id`）的 `Chunk`；chunk_id 统一为 `{doc_id}:{index:04d}`。

### 法律法规分块（`chunking/law.py`）

1. 以“第 X 条”为最小语义单元，识别并携带“编 / 篇 / 章 / 节”上下文到 `heading_path`。
2. 条文不超过硬上限 650 字符：整条作为一个 chunk。
3. 条文超过 650 字符且存在多个自然段：按“款”拆分，`paragraph_no` 标记“第 N 款”；“（一）（二）（三）”等项保留在所属款正文，不作为独立 chunk。
4. 拆分后少于 120 字符的款级 chunk 优先并入相邻款，避免语义过弱。
5. `citation_label` 由“法规标题 + 条号 + 款号”拼成，用于检索评测从“命中 chunk”升级为“命中可引用证据”。

### 结构化分块（`chunking/structured.py`）

面向指南、问答、标准、表格密集材料，硬上限 650 字符、软上限 480 字符、最小 120 字符：

1. `faq` 以“问：”为边界，问题与回答作为一个 chunk。
2. 表格密集文本（HTML 表格或 docling/Markdown 表格）按行拆分，每个起始 chunk 前置表头行，保证“数据类别”等表头上下文不丢失。
3. 其他文本按 Markdown 标题、数字编号标题、中文序号标题分层，过长段落按软上限切分。
4. 装饰性表格碎片（如 `| 个人信息保护政策模版` 这类短 `|` 前缀行）直接丢弃。
5. 结构化材料不携带条款号，`can_cite_clause=False`：可检索、可出现在证据面板，但不可作为结论级条款引用。

### Tiny chunk 合并（`chunking/pipeline.py`）

低于 20 字符的孤立标题/前缀 chunk 合并进相邻文本 chunk（合并后不超过 650 字符），避免索引中出现无检索价值的碎片。

## 文档解析器

`normalize.py` 按文件后缀路由解析器，`--parser` 可选 `auto` / `plain` / `docx` / `docling` / `mineru`：

| 输入 | 默认解析器 | 说明 |
| --- | --- | --- |
| `json` | 内置 JSON 解析器 | 处理 FLK 结构化导出（标题、制定机关、公布/施行日期、法规类型、内容树） |
| `docx` | 标准库 `zipfile` + XML | 按 Word body 顺序处理段落和表格，表格转 HTML 片段；零重依赖 |
| `html` / `htm` | 内置 HTML 解析器 | 去 script/style、块级标签换行，表格单元格以竖线分隔 |
| `pdf`、图片（png/jpg/...） | Docling | 输出 Markdown，保留版面与表格结构 |
| `plain` | 纯文本读取 | 兜底 |
| 显式 `docling` / `mineru` | 对应重型解析器 | 扫描件、复杂版式可用 MinerU pipeline 产出 Markdown |

Docling 配置要点：

1. 表格结构显式使用 TableFormerV2；OCR 默认本地 RapidOCR（`onnxruntime` 后端），可切换到远程 KServe v2-compatible OCR 服务（`LAWAGENT_DOCLING_OCR_ENGINE=kserve_v2_ocr` + `LAWAGENT_DOCLING_OCR_API_URL`）。
2. 模型产物默认缓存在 `data/models/docling/`；若目录缺 RapidOCR 模型，流水线回退到 Docling 默认模型缓存。
3. 单张图片会先包裹成单页 PDF 再走 PDF + OCR pipeline，避免离线时尝试下载 HuggingFace 模型。

## 来源纳入规则

第一版知识库默认只纳入可直接作为合规判断依据或操作指引的现行材料：

1. 修改决定、修正草案、征求意见稿、新闻稿式发布说明不直接进入正式检索知识库；除非做版本沿革说明，否则只作为来源 trace 或人工审阅材料保留。
2. 同一文件存在多个版本时，默认启用最新现行版本；旧版进入 `superseded`/`amended` 状态，不参与默认召回。
3. 一个发布页含多个附件时拆成多个逻辑文档管理，不把发布页标题当作唯一知识文档。
4. 对数据出境材料：`数据出境安全评估申报指南（第三版）` 替代第二版；但同页发布的 `个人信息出境标准合同备案指南（第二版）` 是另一类备案指南，仍保留。
5. 旧版法律、旧版指南、历史决定进入审计/版本对照数据集，回答层默认过滤。
6. 条款级引用按“法律效力 + 条款定位”两个硬条件判定（见上文“引用资格三层模型”）：地方性/行业性条件角色不剥夺真实法规（law/regulation/judicial_interpretation）的条款效力，其适用边界由检索软加权、scope note 与在线语义校验约束；标准、指南、问答、内部制度可被检索或辅助解释，但不能作为“第 X 条/第 X 款”的最终引用来源。

## 评测候选集

`evalset build` 从 `chunks.jsonl` 自动构建检索评测候选（默认 60 条）。这是 ETL 层“出厂质检”：检查数据流水线和 chunk 是否可检索。端到端审查场景的黄金评测集（82 个场景、must-have/optional 人工标注）由 `law_agent/review/evalset/` 维护，不在数据治理流水线内。

## 在线补库入口（`law_agent.kb`）

离线 ETL 流水线之外，受控库的日常入库入口是：

```powershell
python -m law_agent.kb ingest .\新资料.pdf
```

工作台知识库管理与该 CLI 共用 `law_agent/kb/ingestion.py` 这一套边界，复用 normalize → clean → chunk 原语与同一引用资格策略：

1. 按规范化正文查重：同内容即使改文件名也默认跳过；正文变化且标题匹配已有来源时，可更新为新版本，否则作为独立来源。
2. 更新时新 chunk 先以**不可检索**状态写入 ES 与 pgvector，校验一致后才切换为可检索并删除旧 chunk；未变化 chunk 复用 embedding 缓存。
3. Agent 经 `search_web` 发现的新官方来源先进入补库队列（`queue_enrichment` → knowledge-worker），由知识库管理员复核下载、比对与入库；**未通过治理的材料永远不具备引用资格**。

## 验收标准

1. 能从国家法律法规数据库生成数据合规主题的候选 manifest，且只有 `include_in_mvp=true` 进入采集。
2. 所有来源解析为统一 `Document`，`ingest_meta` 记录解析器与版本。
3. 清洗是确定性的，`cleaning_rule_hits` 能说明每类噪声的命中情况。
4. 每个 chunk 继承父文档来源、标题路径、条款号、引用角色、引用资格和前后邻居。
5. 每个 chunk 携带 `citation_role` 与 `can_cite_clause`；条款引用资格按“法律效力 + 条款定位”在 `law_agent/data/citation_policy.py` 判定，在线审查引用门禁共用同一策略；案件适用性走软加权与语义校验，不做硬过滤。
6. 法规正文保真：LLM 只做元数据增强，不改写正文。
7. 所有真实数据默认不进 Git，只提交 schema、样例和报告。
