# LawAgent 服务栈 (Elasticsearch + PostgreSQL/pgvector)

本目录下的 `docker-compose.yml` 用于本地启动 LawAgent 项目真实混合检索所需的两个基础服务：

- **Elasticsearch 8.13.0**：全文检索引擎，容器首次启动会安装 `analysis-ik` 中文分词插件，承担 BM25 / 关键词全文检索（`ik_max_word` 索引 + `ik_smart` 搜索）。
- **PostgreSQL 16 + pgvector**：关系型存储与向量检索，承担语义向量近邻搜索（HNSW）。

## 默认连接信息

| 服务 | 地址 / DSN |
| --- | --- |
| Elasticsearch | http://localhost:9200 |
| PostgreSQL | `postgresql://lawagent:lawagent@localhost:5432/lawagent` |

> 账号密码均为 `lawagent`，仅供本地开发，请勿用于生产。

## 默认语料路径

服务索引默认读取：

```text
data/corpus/legal_docs_20260702/chunks.jsonl
```

该目录是本地生成数据，默认被 git 忽略；如需切换语料，可在 `law_agent.review` 相关命令中传入 `--chunks`。

## 启动服务

在 `docker-compose.yml` 所在目录执行：

```bash
docker compose up -d --build
```

首次构建时 Elasticsearch 镜像会安装 IK 插件（通过 `docker/elasticsearch.Dockerfile` 烘焙进镜像，一次性完成）；数据持久化到命名卷 `esdata`、`pgdata`，重启不丢失。后续启动无需 `--build`，直接 `docker compose up -d` 即可。

查看状态（两个服务的 healthcheck 均为 `healthy` 即可接入）：

```bash
docker compose ps

# 快速验证
curl http://localhost:9200
docker exec -it lawagent-pg psql -U lawagent -d lawagent -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

验证 IK 中文分词是否就绪：

```bash
curl -X POST http://localhost:9200/_analyze \
  -H 'Content-Type: application/json' \
  -d '{"analyzer":"ik_max_word","text":"中华人民共和国民法典"}'

curl -X POST http://localhost:9200/_analyze \
  -H 'Content-Type: application/json' \
  -d '{"analyzer":"ik_smart","text":"中华人民共和国民法典"}'
```

## 中文分词 (IK) 注意事项

- `ik_max_word` 用于索引阶段，最大粒度切分以保证召回；`ik_smart` 用于搜索阶段，智能粒度切分以提升精度。
- IK 插件版本需与 ES 版本匹配；当前使用 ES **8.13.0**，Dockerfile 会下载对应版本的 `analysis-ik`。
- 插件在镜像构建阶段（`docker/elasticsearch.Dockerfile`）安装到镜像层，不在 `esdata` 数据卷内；`docker compose down` 删除容器后再次 `up` 会直接复用已构建的镜像，无需重装。
- 代码运行时会自动检测已安装的插件：优先 IK（`ik_max_word`/`ik_smart`），其次 smartcn，最后回退到 standard analyzer；当前 Dockerfile 安装的是 IK。
- 若后续改用 smartcn 或其他分词器，需修改 `docker/elasticsearch.Dockerfile` 中的安装命令并重新构建 ES 镜像。
- IK 支持自定义词典：将法律专业术语（如"诉讼时效中断""表见代理"等）放入自定义词库文件，可提升分词准确率。

## 停止服务

```bash
# 停止容器但保留数据
docker compose stop

# 删除容器，保留数据卷
docker compose down

# 删除容器并彻底清除数据卷（谨慎，数据将丢失）
docker compose down -v
```
