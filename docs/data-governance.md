# 数据治理

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
python -m law_agent.data report governance
```

配置好 manifest 和 LLM 后，也可以一条命令跑完：

```powershell
python -m law_agent.data pipeline run
```

## 目录结构

CLI 默认中间路径是 `data/manifests/`、`data/raw/`、`data/normalized/`、`data/cleaned/`、`data/enriched/`、`data/chunks/`。当前实际落地的语料包（2026-07-02 快照）在 `data/corpus/legal_docs_20260702/`：

```text
data/corpus/legal_docs_20260702/
  source_manifest.review.csv        # 人工确认后的来源清单
  raw/                              # 下载的原始材料
  documents.normalized.jsonl        # 统一 Document
  documents.cleaned.jsonl           # 清洗后文档
  documents.enriched.jsonl          # 语义增强后文档
  chunks.jsonl                      # 检索分块（含引用角色/引用资格）
  citation_policy.review.csv        # 引用角色逐来源确认表
  fetch_status.csv                  # 采集状态
  collection_summary.json           # 语料统计
  REVIEW_INDEX.md                   # 语料包说明
```

`data/` 整体被 Git 忽略；仓库只保留 `data/manifests/` 下的 schema 与示例 manifest（已显式加入版本控制）。语料在机器间用 `scripts/transfer_corpus.py` 打包传递，见 [开发指南](development.md#3-恢复语料与检索索引)。

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
6. 案例/合同字段（法规数据可为空）：`case_no`、`court`、`trial_instance`、`contract_parties`、`clause_type`
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
3. **案件适用性**（地域、行业、日期）——**不是引用门禁**。检索阶段由 `law_agent/review/retrieval/boosts.py` 做软加权（条件性地方/行业规范在地域或行业匹配时加权、地域明确不符时降权，不按引用角色升降排名，也不过滤），适用边界写入引用的 scope note；在线语义校验再核对已确认事实是否落在该法源的地域/行业/时点范围内（不满足判 unsupported，事实不足判 uncertain）。地方性、行业性规范因此既保留条款效力，其适用边界又始终可见。

各角色含义：

| 角色 | 含义 |
| --- | --- |
| `primary_legal_basis` | 正式依据层（个人信息保护法、数据安全法、出境评估办法、标准合同办法、跨境流动规定等） |
| `conditional_local_basis` | 地方性法规/规章：是否规范按 `doc_type` 判定，角色只标记地域适用边界 |
| `conditional_industry_basis` | 行业性条件规范：同上，角色只标记行业适用边界 |
| `implementation_reference` | 标准、备案指南、模板体系文件，用于落地操作说明 |
| `interpretation_auxiliary` | 答记者问、政策问答、理解与适用，用于解释口径和查询增强 |

每个 chunk 进入检索前都带上 `citation_role` 与按来源预算好的 `can_cite_clause`。

### 元数据一致性规则（`data/source_rules.py`）

类型、效力层级、引用角色、适用地区描述的是同一份文件，并共同决定引用资格，所以必须彼此一致。`metadata_problems` 逐条检查：

| 类型 | 允许的效力层级 | 引用角色 |
| --- | --- | --- |
| `law` 法律 | `national_law` | 主要依据，或按地域/行业的条件依据 |
| `regulation` 法规、规章 | `administrative_regulation`、`departmental_rule`、`local_regulation` | 同上；地方性法规只能是地方性条件依据 |
| `judicial_interpretation` 司法解释 | `judicial_interpretation` | 同法律 |
| `policy` 规范性文件 | `ministry_policy`（部委）、`local_normative_document`（地方，如自贸区数据出境负面清单） | 实施参考或条件依据，不能是主要依据 |
| `guideline` 指南 | `ministry_policy`、`local_normative_document`、`public_interpretation`、`technical_guidance`（如《网络安全标准实践指南》） | 同上 |
| `standard` 国家标准 | `national_standard` | 实施参考 |
| `faq` 问答 | `public_interpretation` | 解释辅助 |
| `contract` 合同范本 | `ministry_policy` | 实施参考 |

另外：只适用于特定地区的法规或规范性文件，引用角色必须是地方性条件依据，并写明地区代码；法律、法规、司法解释必须有发布机关和生效日期（时效以生效日期为准）；其他资料至少要有发布日期，因为它是这份资料唯一的时间依据；发布机关必须是机关名称（“中国（天津）自由贸易试验区”是适用地域，不是机关）；标题不带网页后缀；效力状态与生效日期一致。

发布日期的口径是**首次公开日期**（页面发布时间或官方网址中的日期），没有时才用签署日期近似：时效筛选问的是审查当天能否看到这份文件。

负面清单的定位：各自贸区数据出境管理清单由省级网信办会同有关部门以“关于印发……的通知”发布，依《促进和规范数据跨境流动规定》第六条制定、经省级网络安全和信息化委员会批准并报国家备案，对区内企业有约束力，因此是地方规范性文件（`policy` + `local_normative_document` + 地方性条件依据），不是指南。它不属于法律、法规、规章，不作为条款级引用；引用时法律依据为流动规定第六条，适用的具体条目引清单原文，并注明“地方规范性文件”。

入库（CLI、工作台导入、补库任务）、元数据修改和审核批准都会先过这组规则，任何一条不满足就拒绝发布；`tests/test_source_rules.py` 对已发布的整个语料执行同一组规则，规则或语料一旦不一致，测试即失败。

## 清洗规则

清洗为确定性规则，不改写法规正文。`clean_text` 按顺序执行并记录每类命中数：

1. 移除控制字符、统一换行与空白、折叠重复空行。
2. 去除重复标题行。
3. 移除目录块：法规风格（“目录”标题 + 章节目录重复）与标准风格（点线目录 + 前言/引言边界）两种策略，只有检测到真实目录结构才删除，避免误删正文。
4. Markdown 链接只保留文字、去掉网址（如附件下载链接 `[附件1：标准合同](https://…)` → `附件1：标准合同`）。
5. 移除机械噪声行：图片占位符、Markdown 表格分隔线、PDF 页数行、“可从以下网址获得”来源声明、独立来源 URL、独立年月行、PDF 封面碎片、点线目录行、网页 boilerplate（导航、备案号、打印分享按钮、CMS 信息、页面发布时间行“2024年09月10日 14:30 来源：中国网信网”等）。
6. 修复 PDF 字符间距伪影（如 `D a t a` → `Data`）。
7. 将孤立的条款编号行（如 `3.2`）合并进下一行标题，避免下游产生单字符 chunk。
8. 折叠多余空行。

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

分块按文档类型路由（`chunking/pipeline.py`）：`doc_type` 为 `law`/`regulation` 时走法规分块；`policy`、`judicial_interpretation`、`guideline` 在正文有连续 3 条以上行首“第 X 条”时也按条分块（如粤港澳大湾区标准合同实施指引），条号用于定位和精读，指南仍不能作为条款引用；其余走结构化分块。两种策略都产出带完整来源元数据、`heading_path`、`citation_label`、`citation_role`、`can_cite_clause` 和前后邻居（`prev_chunk_id`/`next_chunk_id`）的 `Chunk`；chunk_id 统一为 `{doc_id}:{index:04d}`。

### 法律法规分块（`chunking/law.py`）

1. 以“第 X 条”为最小语义单元，识别并携带“编 / 篇 / 章 / 节”上下文到 `heading_path`。
2. 条文不超过硬上限 650 字符：整条作为一个 chunk。
3. 条文超过 650 字符且存在多个自然段：按“款”拆分，`paragraph_no` 标记“第 N 款”；“（一）（二）（三）”等项保留在所属款正文，不作为独立 chunk。
4. 拆分后少于 120 字符的款级 chunk 优先并入相邻款，避免语义过弱。
5. `citation_label` 由“法规标题 + 条号 + 款号”拼成，用于检索评测从“命中 chunk”升级为“命中可引用证据”。

### 结构化分块（`chunking/structured.py`）

面向指南、问答、标准、表格密集材料，硬上限 650 字符、软上限 480 字符、最小 120 字符：

1. `faq` 以“问：”为边界，问题与回答作为一个 chunk。
2. 表格密集文本（HTML 表格或 Markdown 表格）一行一块。表格里横跨整行的行是分节标题（如负面清单的“一、医药行业”“（一）需要通过数据出境安全评估的数据清单”），每一行数据前都写上所在分节路径和列名，例如“二、车联网行业 | （二）需要通过个人信息出境标准合同备案、个人信息保护认证出境的数据清单 / 数据类别 | 数据子类 | 数据基本特征与描述 / 个人信息 | 16.……”，分节路径同时写入 `heading_path`。这样单独检索到的一行仍能看出它属于哪个行业、触发哪种程序。本节没有自己的列名行时沿用表格上一个列名；不足 120 字的行并入同节上一行；表格在原文中逐页重复的表头只保留一次。
3. 其他文本按 Markdown 标题、数字编号标题、中文序号标题分层，过长段落按软上限切分。
4. 装饰性表格碎片（如 `| 个人信息保护政策模版` 这类短 `|` 前缀行）直接丢弃。
5. 结构化材料不携带条款号，`can_cite_clause=False`：可检索、可出现在证据面板，但不可作为结论级条款引用。
6. 国家标准（GB/T）的 `doc_type` 为 `standard`、`authority` 为 `national_standard`，与指南、负面清单区分。

### Tiny chunk 合并（`chunking/pipeline.py`）

低于 20 字符的孤立标题/前缀 chunk 合并进相邻文本 chunk（合并后不超过 650 字符），避免索引中出现无检索价值的碎片。

## 文档解析器

`normalize.py` 按文件后缀路由解析器，`--parser` 可选 `auto` / `plain` / `docx` / `docling` / `mineru`：

| 输入 | 默认解析器 | 说明 |
| --- | --- | --- |
| `json` | 内置 JSON 解析器 | 处理 FLK 结构化导出（标题、制定机关、公布/施行日期、法规类型、内容树） |
| `docx` | 标准库 `zipfile` + XML | 按 Word body 顺序处理段落和表格，表格转 HTML 片段；纵向合并单元格（`vMerge`）的续格填入被合并的文字（负面清单的“数据类别”列就是这样写的）；零重依赖 |
| `html` / `htm` | `data/html_content.py`（标准库 `html.parser`） | 只取正文：按标签（`nav`/`header`/`footer`/表单/脚本）和 `class`/`id`（nav、menu、footer、share、breadcrumb 等）去掉站点导航、账号入口、分享和页脚，保留仍包含页面大部分正文的最小元素；表格按 `<table>` 行输出，供表格切块使用；按页面声明或探测的编码解码（政府网站常用 GBK） |
| `pdf` | pdfplumber（`data/pdf_layout.py`） | 读文本层及其版面：表格按单元格网格读出并与正文按阅读顺序排列；被纵向合并的单元格取上方格的值；跨页表格合为一张表，去掉逐页重复的表头，被分页截断的行拼回；正文中在右边距折行的行重新接回；去掉 PDF 在汉字之间、汉字与数字之间插入的空格（行首节号如“3.12 转让”保留）；页码行去掉；页脚的小号脚注移到全文末尾，使跨页句子接上。文本层缺失或不可用时才走 Docling OCR |
| 图片（png/jpg/...） | Docling | OCR，输出 Markdown |
| `plain` | 纯文本读取 | 兜底 |
| 显式 `docling` / `mineru` | 对应重型解析器 | 扫描件、复杂版式可用 MinerU pipeline 产出 Markdown |

Docling 配置要点：

1. 表格结构显式使用 TableFormerV2；OCR 默认本地 RapidOCR（`onnxruntime` 后端），可切换到远程 KServe v2-compatible OCR 服务（`LAWAGENT_DOCLING_OCR_ENGINE=kserve_v2_ocr` + `LAWAGENT_DOCLING_OCR_API_URL`）。
2. 模型产物默认缓存在 `data/models/docling/`；若目录缺 RapidOCR 模型，流水线回退到 Docling 默认模型缓存。
3. 单张图片会先包裹成单页 PDF 再走 PDF + OCR pipeline，避免离线时尝试下载 HuggingFace 模型。

## 来源纳入规则

知识库默认只纳入可直接作为合规判断依据或操作指引的现行材料：

1. 修改决定、修正草案、征求意见稿、新闻稿式发布说明不直接进入正式检索知识库；除非做版本沿革说明，否则只作为来源 trace 或人工审阅材料保留。
2. 同一文件存在多个版本时，默认启用最新现行版本；旧版进入 `superseded`/`amended` 状态，不参与默认召回。
3. 一个发布页含多个附件时拆成多个逻辑文档管理，不把发布页标题当作唯一知识文档。
4. 对数据出境材料：`数据出境安全评估申报指南（第三版）` 替代第二版；但同页发布的 `个人信息出境标准合同备案指南（第二版）` 是另一类备案指南，仍保留。
5. 旧版法律、旧版指南、历史决定进入审计/版本对照数据集，回答层默认过滤。
6. 条款级引用按“法律效力 + 条款定位”两个硬条件判定（见上文“引用资格三层模型”）：地方性/行业性条件角色不剥夺真实法规（law/regulation/judicial_interpretation）的条款效力，其适用边界由检索软加权、scope note 与在线语义校验约束；标准、指南、问答、内部制度可被检索或辅助解释，但不能作为“第 X 条/第 X 款”的最终引用来源。

## 评测

评测场景与标签由 `law_agent/evals/` 维护，不在数据治理流水线内；`python -m law_agent.evals check` 会对照当前 `chunks.jsonl` 检查标签中的来源、条号和时效，见 [评测](evaluation.md)。

## 在线补库入口（`law_agent.kb`）

离线 ETL 流水线之外，受控库的日常入库入口是：

```powershell
python -m law_agent.kb ingest .\新资料.pdf
```

工作台知识库管理与该 CLI 共用 `law_agent/kb/ingestion.py` 这一套边界，复用 normalize → clean → chunk 原语与同一引用资格策略：

1. 按规范化正文查重：同内容即使改文件名也默认跳过；正文变化且标题匹配已有来源时，可更新为新版本，否则作为独立来源。
2. 更新时新 chunk 先以**不可检索**状态写入 ES 与 pgvector，校验一致后才切换为可检索并删除旧 chunk；未变化 chunk 复用 embedding 缓存。
3. Agent 经 `search_web` 发现的新官方来源先进入补库队列（`queue_enrichment` → knowledge-worker），下载后走同一条解析、清洗、切块和入库门禁；**未通过治理的材料永远不具备引用资格**。
4. 新来源的元数据由模型读取（`kb/source_llm.py`），三条入口（knowledge-worker、CLI、工作台导入）共用 `read_source_metadata`：
   - 模型读文件开头和结尾，按发布形式而不是标题字眼判断类型、效力层级、引用角色、适用地区、发布机关和日期，每个字段附一段原文。
   - 不要求每个字段都填：看不出来的填空；有倾向但拿不准的，给出最可能的判断供参考，同时标为不确定并写明原因。
   - 原文核验只判断引文是否属实，不替模型下结论：引文须出现在标题、网址或正文中（允许用省略号连接的分段），日期须能从引文、页面地址或检索日期读出，发布机关须出现在引文中，法律、法规、司法解释须引到相应的发布形式（令号、主席令、法释等）。
   - 待人工确认的字段：模型标为不确定的、没填的、取值无效的、引文不成立的。
   - 操作者已填写类型、效力层级、引用角色和发布机关时不调用模型。
   - 模型未配置或调用失败时，由 `kb/source_inference.py` 的规则推断预填草稿，草稿标明“规则推断”，一律交人工审核，不自动发布。规则如下：
     - 类型与效力层级：发文字号（“令第X号”“第X号令”，含多部门联合令）判为部门规章；全国性“……条例”判为行政法规，地名“……条例”加人大常委会公告判为地方性法规；“法释〔〕”判为司法解释；全国人大网站或正文含主席令的“中华人民共和国……法”判为法律；“GB/T”判为国家标准；“答记者问”“问答”判为问答；“清单”“指南”“指引”判为指南；“通知”“办法”等无令号的判为规范性文件。令号只在页面标题和正文开头（第一条之前）查找，正文中引用的其他规章不算；“主席令”“国务院令”不算部门令。令号的各种写法（“令（第9号）”“令〔2025〕第3号”“令【第218号】”）都识别。标题含“答记者问”等先判为问答，即使正文提到实践指南；《网络安全标准实践指南》判为标准化技术文件。
     - 引用角色与适用地区：标题含地区（如“浙江自贸试验区”）为地方性条件依据并给出地区代码（CN-ZJ），含行业（汽车、金融等）为行业性条件依据。
     - 日期：发布日期依次取标准的“发布/实施”行、页面发布时间、发布令的签署日期、官方页面地址中的日期、检索结果日期；生效日期取“自X年X月X日起施行”，“自发布之日起施行”取发布日期。修正后的法律文本（“根据……修正”）不推断生效日期，交审核人核对修改决定。
     - 发布机关：取发文字号或落款中的机关（地方文件识别“北京市互联网信息办公室”这类省市机关）；问答、指南等正文未署名时取发布网站所属机关（cac.gov.cn → 国家互联网信息办公室）。
     - 标题去掉网页后缀（“_中国政府网”）和“（全文）”，版本标识（`instrument_key`）为去掉“（第三版）”“（2025版）”“（试行）”“（2024修订）”后的标题。

     每项推断都记下依据（如“正文含部门规章令号‘国家互联网信息办公室令第16号’”）；正文无法确定的字段列为待定。操作者明确填写的字段优先于推断。
5. 新版本按版本标识匹配已入库的旧版本，不要求同一网址。新版日期晚于旧版时，入库后旧版自动设置失效日期（`valid_to` 为新版生效日），时效筛选随之以新版为准；新版日期不晚于旧版或日期缺失时，不入库，交审核人核实版本关系。
6. knowledge-worker 自动发布的范围：法律、法规、司法解释，且由模型读取、没有待确认字段、元数据符合一致性规则、解析通过门禁、切块后有条款结构、版本关系清楚。其余来源（指南、问答、清单、标准等）、规则推断的草稿以及任何有待确认字段的来源进入待审，审核页预填读取结果，逐项显示原文依据、待确认字段、模型说明、将取代的旧版本和切块预览，审核人确认或修改后批准。CLI 有待确认字段时，交互模式下展示后确认，非交互模式下要求用 `--metadata` 补充。
7. 2026-11-23 用 27 个未入库的官方页面快速对比：规则在转载页和非常见令号写法上会确定地判错；模型的判断大多正确，拿不准的基本都被标为待确认，因此改为由模型读取、规则兜底。

## 验收标准

1. 能从国家法律法规数据库生成数据合规主题的候选 manifest，且只有 `include_in_mvp=true` 进入采集。
2. 所有来源解析为统一 `Document`，`ingest_meta` 记录解析器与版本。
3. 清洗是确定性的，`cleaning_rule_hits` 能说明每类噪声的命中情况。
4. 每个 chunk 继承父文档来源、标题路径、条款号、引用角色、引用资格和前后邻居。
5. 每个 chunk 携带 `citation_role` 与 `can_cite_clause`；条款引用资格按“法律效力 + 条款定位”在 `law_agent/data/citation_policy.py` 判定，在线审查引用门禁共用同一策略；案件适用性走软加权与语义校验，不做硬过滤。
6. 法规正文保真：LLM 只做元数据增强，不改写正文。
7. 所有真实数据默认不进 Git，只提交 schema、样例和报告。
