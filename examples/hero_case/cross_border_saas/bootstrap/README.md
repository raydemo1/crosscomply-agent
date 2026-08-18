# 官方来源 bootstrap 说明

`official_sources.csv` 是本案例的**最小来源清单**，不是法源正文包，也不会自动替代仓库知识库 manifest。它的用途是让演示者在新环境中核对必需来源是否存在、`source_id` 是否稳定、官方页面是否仍可访问。

建议流程：

1. 启动本地 PostgreSQL、Elasticsearch 和知识库服务；
2. 运行 `python -m law_agent.kb list`，确认 `required_for_demo=true` 的来源已经存在；
3. 缺失时，从 CSV 中的官方页面人工核验并下载正式文件；
4. 使用仓库统一入口 `python -m law_agent.kb ingest <文件>` 导入；批处理时为每个文件提供人工复核后的 metadata；
5. 运行 `python -m law_agent.review service-doctor`，确认 Elasticsearch 与 PostgreSQL 均健康且索引计数非零；
6. 抽查来源标题、`source_id`、引用角色、现行状态和官方 URL，不仅检查“是否有数据”。

注意：

- 不要把 CSV 中的页面标题或 URL 当作已下载、已解析的正文；
- 不要提交 API Key、飞书凭据、完整本地语料、向量或数据库转储；
- `interpretation_auxiliary` 和 `implementation_reference` 只能用于解释或实施说明，不应冒充条款级正式依据；
- 官方页面和材料版本可能变化，正式演示前仍需人工核验。本清单记录的是案例基线，不承诺永久有效。
