# 多台电脑开发

每台电脑使用独立工作区、`.env` 和 Docker 数据卷；代码通过 Git 同步，法律语料通过带校验清单的压缩包同步。案件数据库、附件、密钥和历史评测记录不包含在语料包中。

## 新电脑准备

安装 Git、Python 3.11+、Node.js 22.12+、PowerShell 7 和 Docker Desktop（Linux containers），然后在仓库根目录执行：

```powershell
git clone https://github.com/raydemo1/crosscomply-agent.git
cd crosscomply-agent
python -m pip install -e ".[dev,service]"
pwsh -File scripts/setup-dev.ps1
Push-Location frontend
npm ci
Pop-Location
```

`setup-dev.ps1` 仅在 `.env` 不存在时创建文件，自动生成随机数据库和 MinIO 密码，并同步宿主机连接配置。模型密钥初始留空，需要真实检索或审查时再填写。已有 `.env` 不会被覆盖。

## 还原语料

将语料 ZIP 和同名 `.sha256` 文件复制到新电脑。核对 SHA256 后，将 ZIP 解压到仓库根目录：

```powershell
$archive = 'D:\transfer\crosscomply-corpus-20261103.zip'
$expected = ((Get-Content -LiteralPath "$archive.sha256" -Encoding UTF8).Trim() -split '\s+')[0]
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw '语料压缩包校验失败'
}
Expand-Archive -LiteralPath $archive -DestinationPath .
python scripts/transfer_corpus.py verify
```

包内保留当前 `chunks.jsonl`、法源清单、原始文件、知识库状态及向量缓存；不带补库暂存任务和历史备份。每个文件都记录 SHA256。仅向新目录解压，不覆盖另一套正在使用的语料。

## 启动本地开发依赖

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres elasticsearch minio minio-init
python -m alembic upgrade head
python scripts/transfer_corpus.py restore --execute
```

恢复命令先验证全部文件和向量，再将缓存向量及原有来源 generation 写入 ES/pgvector，不调用任何模型，不要求 Embedding API key。目标检索索引必须为空；有旧数据时明确退出，不删除或覆盖。恢复中途失败时，不要启动 Worker，先检查目标两个索引的状态。

包内的 Embedding 模型名记录打包机器当时的配置，向量维度和每条缓存的处理签名也会校验；目标机器必须使用相同模型和维度。`index-service --execute` 是另一个入口，可能调用 Embedding 服务；恢复已有语料应使用上述 `restore` 命令。

开发 Compose 额外将 MinIO API/控制台绑定到本机 `9000`/`9001`，以供宿主机后端访问；PG/ES 同样只绑定本机。生产 Compose 不发布 MinIO 端口。

首次创建管理员：

```powershell
$env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD = '替换为至少12位的强密码'
python -m law_agent.review.bootstrap_admin --username admin@example.com --display-name '开发管理员'
Remove-Item Env:CROSSCOMPLY_BOOTSTRAP_ADMIN_PASSWORD
```

分别在三个终端运行：

```powershell
# 终端一：API
python -m law_agent.review serve --host 127.0.0.1 --port 8000

# 终端二：处理审查和问答任务；配置真实密钥后，领取任务可能产生模型费用
python -m law_agent.review.worker

# 终端三：前端
cd frontend
npm run dev
```

入口为 `http://127.0.0.1:5173`。真实向量检索需要 Embedding key，审查和问答需要 LLM key。日常开发不必启动自动补库的 `knowledge-worker`。

```powershell
# 已配置检索密钥时执行服务体检；该命令本身不生成向量
python -m law_agent.review service-doctor
```

## MinIO 镜像拉取 401

镜像仓库返回 401 发生在服务启动前，修改 `MINIO_ROOT_PASSWORD` 不会解决。可以用已有镜像离线搬运：

```powershell
# 原电脑：需要 Docker 引擎运行，且本地已存在这两个镜像
docker save -o crosscomply-minio-images.tar quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z quay.io/minio/mc:RELEASE.2025-04-08T15-39-49Z

# 新电脑
docker load -i crosscomply-minio-images.tar
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --pull never minio minio-init
```

也可以在 `.env` 设置 `MINIO_IMAGE`、`MINIO_MC_IMAGE` 为已验证可访问的可信镜像及固定版本，再拉取。示例默认仍使用原固定版本；不能保证换一个镜像域名就能解决当前网络的 401。

## 密码与日常协作

- 本地 Python 读取 `PG_DSN`；Compose 后端使用 `POSTGRES_PASSWORD` 拼接连接串。两者密码必须相同。已有数据库卷不会因为改 `.env` 自动修改账户密码。
- `CROSSCOMPLY_MINIO_ACCESS_KEY` / `SECRET_KEY` 必须对应 MinIO 的实际账户；初始化脚本会生成一致的配置。
- `.env`、`data/` 和 Docker 数据卷均不通过 Git 同步。新电脑默认新建案件库；如需旧案件，还须另行备份恢复 PostgreSQL 和 MinIO。
- 两台电脑用不同分支，开始工作前拉取最新代码，提交后推送。不要直接共享一个工作目录或 Docker 数据目录。
- `docker compose down` 保留数据卷；不要用 `down -v` 排查密码或连接问题。

## 后续重新打包

停止可能修改语料的补库任务后，在仓库根目录运行。脚本不覆盖已存在的压缩包：

```powershell
python scripts/transfer_corpus.py pack --output output/transfer/crosscomply-corpus-20261103.zip
```

打包后会读取 ZIP 验证逐文件散列，并生成同名 `.sha256` 文件。代码提交号写入包内清单，便于两台机器对齐版本。
