# 从上传材料到决策报告：英雄案例操作说明

这份说明用于稳定演示完整业务闭环，不要求重新运行成本较高的 LLM/RAG 评测。应用功能仍在并行产品化时，可先用本目录固定资产讲解；接口和 UI 已落地后，再把同一材料按步骤上传。

## 0. 校验案例包

在仓库根目录运行：

```powershell
pwsh -File .\examples\hero_case\cross_border_saas\verify_integrity.ps1
```

预期输出以 `PASS hero case integrity` 结束。若材料哈希不一致，不要修改旧快照记录来“消除错误”，而应建立新的材料版本和快照。

## 1. 检查官方来源

```powershell
python -m law_agent.kb list
python -m law_agent.review service-doctor
```

对照 `bootstrap/official_sources.csv`，确认 `required_for_demo=true` 的稳定 `source_id` 已存在，Elasticsearch 与 PostgreSQL 健康。缺失材料只能从官方页面核验、下载后通过统一 `law_agent.kb ingest` 入口导入；不要把来源清单当正文。

## 2. 创建案件并上传首轮材料

创建案件 `CC-2026-DEMO-001`，上传：

1. `materials/01_procurement_application.md`；
2. `materials/02_vendor_dpa_excerpt.md`。

系统应保存原件版本、SHA-256、上传人、解析状态与解析文本，并为本次运行绑定不可变快照。演示讲解时可直接对照 `review/model_pre_extraction.json`，但必须说明它是固定模型输出样例，不是本次在线模型性能证明。

## 3. 展示“未知不推测”

首轮预抽取应明确保留以下未知：CIIO、重要数据、累计个人信息人数、累计敏感个人信息人数、法定豁免和供应商处理链信息。确定性规则不能在关键事实未知时给出可送审路径，案件进入“待补件”。

这一幕是产品差异点：Agent 负责提取和解释，规则引擎负责硬条件计算，任何未知关键事实都不能由模型补全。

## 4. 补件与逐项人工确认

上传：

1. `materials/03_data_inventory.csv`；
2. `materials/04_security_questionnaire.md`。

确认人按 `review/fact_confirmation_record.md` 逐项核对材料定位，再形成 `review/human_confirmed_facts.json` 对应的 V2 快照。重点展示人数是跨数据集按自然人去重，沟通内容与联系人不能重复累加。

## 5. 运行全国主路径规则

使用 V2 快照触发规则判定。预期结果见 `review/rule_decision.json`：

- 不命中安全评估路径；
- 不命中本案例可适用的法定豁免；
- 产生“个人信息出境标准合同”和“个人信息保护认证”两个候选路径；
- 本次由法务人工选择标准合同路线；
- FTZ、粤港澳和行业特例保留为 RAG 检索与人工确认事项，不由硬规则擅自扩张。

同一材料快照与规则版本应产生同一路径结果。

## 6. 证据化深审与整改

深审基于正式依据、实施参考和解释辅助的不同引用角色生成建议。对照 `review/remediation_checklist.csv` 展示：

- 哪些动作阻断生产上线；
- 每个动作的责任角色和证据要求；
- 模型报告不能替代标准合同、个人信息保护影响评估或备案材料；
- 数据、目的、接收方、地点或重要数据判断变化会触发重审。

## 7. 创建飞书审批并回写

按 `approval/feishu_approval_demo.md` 在测试企业中创建审批。通过分支使用 `fi_demo_cc_2026_001` 作为展示 ID，真实运行时必须保存飞书实际返回的实例 ID。

演示三个断言：

1. 回调先验签，再按事件 ID 幂等处理；
2. 网络失败进入持久化重试，本地不能提前显示通过；
3. 飞书通过、拒绝或撤回的终态不能被本地越权覆盖。

`approval/feishu_event_fixtures.json` 是无签名、不可发送的展示数据，只用于讲解字段和重复投递场景。

## 8. 导出并校验报告

报告渲染输入见 `report/report_data.json`，可读示例见 `report/decision_report_example.md`。正式 PDF 应至少包含案件编号、材料哈希、规则版本、法源、整改项、审批人、审批时间和审批实例 ID。

生成 PDF 后：

1. 计算 PDF SHA-256；
2. 将哈希写入持久化报告记录；
3. 从案件页下载并重新计算；
4. 比对一致后展示“校验通过”。

本案例没有预制 PDF，因此 `pdf_sha256` 明确为 `null`，避免把 Markdown 文件冒充正式 PDF。

## 演示完成标准

- 首轮关键事实未知时不能送审；
- 补件后每项关键事实都能追到具体材料、版本、哈希、确认角色和时间；
- 同一快照和规则版本给出相同候选路径；
- 飞书决定能追到材料、规则、法源和整改项，重复事件不会生成重复决定；
- 报告哈希可独立校验；
- 全程不展示密钥、真实个人信息、完整本地语料或未经人工确认的模型结论。
