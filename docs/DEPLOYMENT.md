# AER 部署手册（Deployment Guide）

> 本文只讲一件事：**如何把一个 Git commit 变成一个在 Linux 服务器上跑起来的 AER 运行时，并且随时能退回去。**

英文术语第一次出现时附中文解释。所有命令都可在生产服务器上直接执行。

---

## 1. 先理解一件事：AER 没有常驻服务

AER 是**嵌入式运行时（embedded runtime）**：一个 Python 库，加上一个 SQLite 文件。它不监听端口、没有 HTTP 接口、没有 healthcheck 端点（endpoint，可轮询的健康检查地址），也没有需要守护的进程。

所以本项目的 "部署"（deployment，发布上线）**不是**启动一个服务，而是：

```text
构建不可变镜像
   ↓
发布到镜像仓库（registry）
   ↓
生产服务器拉取（pull）该镜像
   ↓
备份 SQLite
   ↓
在容器内执行数据库迁移（migration）
   ↓
生产冒烟测试（smoke test）
   ↓
记录当前版本（current.env）
```

任何一步失败 → 部署失败，且**不会**记录新版本。宁可停在旧版本，也不要记录一个未经证实的版本。

**为什么不加一个 FastAPI（Python Web 框架）+ `/health`？** 因为那会凭空造出一个没有业务意义的服务：它唯一的作用是证明自己还活着，而它本身没有任何东西可做。真要有常驻进程（例如未来的 Agent worker），那时再加也不迟。

---

## 2. 架构总览

```text
┌──────────────────────────┐
│ GitHub Actions (CI)      │  拉取代码 → 质量门禁 → 构建镜像 → 推送到 GHCR
└────────────┬─────────────┘
             │ docker push        镜像标签 = sha-<commit>
             ▼
┌──────────────────────────┐
│ GHCR                     │  ghcr.io/<owner>/<repo>:sha-<commit>
│ (GitHub 容器镜像仓库)      │
└────────────┬─────────────┘
             │ docker pull（服务器用一次性只读凭据）
             ▼
┌──────────────────────────────────────────────────────┐
│ 生产服务器 bwg-06 (Linux x86_64)                      │
│                                                      │
│  /srv/aer/deploy/  compose.yaml / .env / current.env │
│                    deploy.sh  / rollback.sh          │
│  /srv/aer/data/          ← 真正的数据（bind mount）    │
│  /srv/aer/backups/       ← 迁移前的备份                │
│  /srv/aer/artifacts/     ← 产物                      │
│  /srv/aer/knowledge/     ← 预留：未来 NeuG 索引        │
│  /srv/aer/logs/          ← 部署步骤日志                │
│                                                      │
│  容器（非 root，uid 10001）挂载上述目录后执行：          │
│    backup_sqlite.py → alembic upgrade head           │
│                     → smoke_test.py                  │
└──────────────────────────────────────────────────────┘
```

关键点：**服务器不构建任何东西**（不 `git clone`、不 `pip install`、不 `docker build`）。它只拉取一个由 CI 构建好的不可变镜像。这样"构建一次，部署同一份制品"（build once, deploy the same artifact）才真正成立。

---

## 3. 服务器要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Linux x86_64 | 目标节点 `bwg-06` |
| Docker Engine | 24+ | 提供 `docker` 命令 |
| Docker Compose | v2 插件（`docker compose`） | 注意是子命令形式，不是 `docker-compose` |
| Python | **不需要** | 所有脚本都在容器里跑 |
| 磁盘 | 数据 + 备份 × 2 倍余量 | 默认保留最近 20 份备份 |
| 网络 | 可访问 `ghcr.io` | 拉取镜像 |

服务器上不需要 Python 是一个**刻意**的设计：宿主机装一套 Python/Alembic 会导致"宿主机代码版本 ≠ 容器迁移版本"，那是很隐蔽的故障源。

---

## 4. 目录布局

```text
/srv/aer/
├── data/          aer.db              ← 唯一不可替代的东西
├── artifacts/                         ← 产物（当前为空）
├── knowledge/                         ← 预留：未来 NeuG 的 knowledge.db
├── backups/       aer-<时间>-<sha>.db ← 迁移前备份 + sidecar JSON
│                  aer-<时间>-<sha>.db.json
├── logs/          deploy.log          ← 部署步骤日志
└── deploy/
    ├── compose.yaml   ← 由 CI 同步（来自被验证的 commit）
    ├── deploy.sh      ← 由 CI 同步
    ├── rollback.sh    ← 由 CI 同步
    ├── env.example    ← 由 CI 同步（模板，供参考）
    ├── .env           ← 由你手工创建，CI **永不覆盖**
    └── current.env    ← 由 deploy.sh 写入：当前/上一个镜像
```

`knowledge/` 目前**故意为空**：它是下一个里程碑（NeuG 知识索引）的预留挂载点。本轮不安装任何东西进去。

### 属主（Ownership）

容器以 **uid/gid 10001** 运行。Docker 不会做 uid 映射，所以宿主机目录必须由 10001 拥有：

```bash
sudo chown -R 10001:10001 /srv/aer/{data,artifacts,knowledge,backups}
sudo chmod 750 /srv/aer/{data,artifacts,knowledge,backups}
```

`deploy.sh` 在以 root 运行时会自动做这个 `chown`。**不要用 `chmod 777`**：把生产数据目录设为全局可写并不是解决问题，而是把问题扩大。

---

## 5. 镜像身份（Image identity）

每个生产镜像必须能回答："线上跑的是哪个 commit？"

镜像上带有 OCI 标签（label，镜像元数据）：

```text
org.opencontainers.image.revision → <git commit>
org.opencontainers.image.source   → <仓库地址>
org.opencontainers.image.version  → <ref 名>
org.opencontainers.image.created  → <构建时间>
```

查询方式：

```bash
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' <镜像>
```

注意 `org.opencontainers.image.source` 不是装饰：GHCR 靠它在镜像包与仓库之间建立关联，CI 会自动填成 `github.server_url/github.repository`。

### 为什么只有 `sha-<commit>` 这一个标签

本项目**只发布** `ghcr.io/<owner>/<repo>:sha-<commit>`，不发布 `latest`。

`latest` 的含义是"最后一次推送的东西"——正是回滚时最需要知道、却最不知道的信息。多一个可变名字，就多一条"部署了非预期版本"的路径。想要便利，就读 `current.env`。

`deploy.sh` 会**拒绝**任何 `:latest` 或没有标签的镜像引用。

---

## 6. GitHub 仓库配置

### 6.1 Secrets（机密）

在 `Settings → Secrets and variables → Actions` 添加，全部放在 **Environment `production`** 下：

| Secret | 说明 |
| --- | --- |
| `PRODUCTION_HOST` | 服务器 IP 或域名 |
| `PRODUCTION_USER` | SSH 用户（建议专用部署账户，不要 root） |
| `PRODUCTION_PORT` | SSH 端口；留空则用 22 |
| `PRODUCTION_SSH_KEY` | 该用户的私钥（OpenSSH 格式） |
| `PRODUCTION_KNOWN_HOSTS` | 服务器主机公钥行，用于**主机密钥校验** |

生成 `PRODUCTION_KNOWN_HOSTS`（在可信网络里执行一次）：

```bash
ssh-keyscan -p 22 -t ed25519 <host>   # 核对指纹后再粘贴进去
```

CI 的 Deploy 步骤会检查该 secret 非空；**空值直接失败**。本项目不允许 `StrictHostKeyChecking=no`：那等于放弃对"服务器是否是伪装者"的判断。

GHCR 推送用的是 GitHub 自带的 `GITHUB_TOKEN`，不需要额外建 PAT（personal access token，个人访问令牌）。

### 6.2 Environment（环境）

`deploy.yml` 的部署任务声明了 `environment: production`。可在 `Settings → Environments → production` 打开 **Required reviewers**（必需审批人）——打开后每次生产部署都需要人工批准。架构已支持，本轮不强制开启。

### 6.3 分支保护与分支名（必须手工配置）

以下属于 **Manual GitHub Repository Configuration**（仓库设置，无法靠仓库里的代码保证）：

- **默认分支名必须是 `main`**：`deploy.yml` 监听 `push: branches: [main]`。若仓库当前
  使用 `master`，请在 GitHub 上改名（或同步修改 `deploy.yml` 的 `branches`）。
  首次推送前 `workflow_dispatch` 不会出现在 Actions 列表里——需要先把 workflow 文件
  推到默认分支。
- 仓库需要配置 `origin` remote 并完成首次推送（CI/CD 都以远端仓库为前提）。
- `main` 禁止 force push（强制推送）
- `main` 只能通过 Pull Request 合并
- 要求 `ci / quality gate` 通过（required status check）
- 建议：要求分支为最新（require branches to be up to date）

### 6.4 服务器侧的镜像仓库凭据（零长期凭据）

**服务器上不保存任何长期凭据。** 私有镜像的拉取由部署工作流自己解决：它用本次运行自带的
`GITHUB_TOKEN`（`permissions: packages: write` 隐含读权限）登录服务器，token 通过
**stdin** 管道进入 `docker login`，既不进命令行（进程列表 / shell 历史）、也不进 CI 日志。
该 token 在本次 job 结束后过期，服务器 `~/.docker/config.json` 里留下的是一份**已失效**
的凭据。

代价与应对：

- **自动部署永远可用**：每次部署都会重新登录一次。
- **服务器上手工执行 `deploy.sh`**：因为凭据多半已过期，拉取会失败。
  `deploy.sh` 会明确提示这一点（"凭据可能已过期，部署工作流会刷新它"）。
  需要手工部署时，先触发一次工作流，或在服务器上自行 `docker login ghcr.io`。
- **`rollback.sh` 不受影响**：回滚目标镜像通常已在本机，脚本在拉取失败时会
  自动退回本地副本（这正是事故现场最需要的行为），只在本地也没有时才报错。

如果你更希望服务器长期具备拉取能力，可以改为自备一个只勾 `read:packages` 的
classic PAT 并手工登录一次；本项目默认**不**这么做，因为那等于在服务器上常驻一个凭据。

---

## 7. 首次部署（First bootstrap）

```bash
# 1) 安装 Docker Engine 与 compose 插件（按官方文档操作；不要用 curl | bash 脚本）
docker --version && docker compose version

# 2) 建立目录
sudo mkdir -p /srv/aer/{data,artifacts,knowledge,backups,logs,deploy}
sudo chown -R 10001:10001 /srv/aer/{data,artifacts,knowledge,backups}
sudo chmod 750 /srv/aer/{data,artifacts,knowledge,backups}

# 3) 写入配置（从仓库取模板，然后编辑）
cp deploy/env.example /srv/aer/deploy/.env
$EDITOR /srv/aer/deploy/.env
chmod 600 /srv/aer/deploy/.env

# 4) 登录 GHCR（见 6.4）

# 5) 触发部署：GitHub → Actions → deploy → Run workflow
```

首次部署时 `deploy.sh` 会发现数据库不存在，**跳过备份**并打印一行说明——这不是错误。随后它会创建数据库、迁移到 head、跑冒烟测试，最后写 `current.env`。

---

## 8. 常规部署

**自动**：合并（merge）到 `main` 即触发，无需手工操作。

**手工指定版本**（重新部署一个已有的镜像，不重新构建）：

```text
Actions → deploy → Run workflow
  image   = ghcr.io/<owner>/<repo>:sha-<commit>
  git_sha = <commit>
```

**直接在服务器上执行**（排障或紧急发布）：

```bash
cd /srv/aer/deploy
IMAGE=ghcr.io/<owner>/<repo>:sha-<commit> GIT_SHA=<commit> bash ./deploy.sh
```

先看它会做什么（不产生任何修改）：

```bash
bash ./deploy.sh --image <镜像> --git-sha <sha> --dry-run
```

部署步骤输出带 UTC 时间戳，同时写入 `/srv/aer/logs/deploy.log`。

---

## 9. 备份

`deploy.sh` 在**迁移之前**自动备份一次。也可以手工执行：

```bash
cd /srv/aer/deploy
docker compose -f compose.yaml run --rm aer-runtime \
  python /app/scripts/backup_sqlite.py \
  --source /data/aer.db \
  --git-sha "$(sed -n 's/^AER_GIT_SHA=//p' current.env)" \
  --print-path
```

为什么不用 `cp aer.db backup.db`：AER 用 WAL（write-ahead log，预写日志）模式，已提交的数据可能还在 `aer.db-wal` 里。只复制主文件得到的是"某次 checkpoint 时的旧状态"——它能被正常打开、不会报错，因此是最危险的一种备份。脚本使用 SQLite 官方的在线备份 API（`sqlite3.Connection.backup()`），在数据库使用中也能拿到一致快照。

每份备份都会：

1. 通过 `PRAGMA integrity_check`（完整性检查）；**不通过就删除该文件并让部署失败**——未经校验的备份比没有备份更糟，因为它会被信任；
2. 生成同名 `.json` sidecar（伴生元数据）：`git_sha` / `image` / `alembic_revision` / `created_at` / `integrity_check`；
3. 按数量保留最近 N 份（默认 20，见 `AER_BACKUP_RETENTION`），旧的连同 sidecar 一起删除。

---

## 10. 回滚（Rollback）

```bash
cd /srv/aer/deploy
bash ./rollback.sh              # 回到 current.env 里记录的 AER_PREVIOUS_IMAGE
bash ./rollback.sh --dry-run
bash ./rollback.sh --to ghcr.io/<owner>/<repo>:sha-<commit>
```

回滚**只切换镜像**，不动数据库。

**为什么不自动执行 `alembic downgrade`？** 降级（downgrade）是*新*版本写的数据、由*旧*版本定义的逆向操作，实战中几乎无法预先验证，而 AER 后续里程碑会产生不可再生的数据（从真实执行中提炼的经验）。因此"应用回滚"和"数据库恢复"是**两个独立决定**，后者显式地从备份恢复。

如果旧镜像读不了当前 schema，`rollback.sh` 会：

1. 报告失败并**拒绝更新 `current.env`**（一个说谎的 `current.env` 比没有更糟）；
2. 打印明确的恢复命令，包括刚才那次迁移前备份的路径。

`--force` 用于你已手工恢复数据库、只需要固定镜像版本的场景。

---

## 11. 从备份恢复（Restore）

这是本仓库最危险的操作，所以脚本刻意设计得"麻烦"：`--source-backup` 与 `--target` **都必须显式给出**，没有"自动挑最新"。

```bash
cd /srv/aer/deploy
# 先看清楚要做什么（校验备份，不写任何东西）
docker compose -f compose.yaml run --rm aer-runtime \
  python /app/scripts/restore_sqlite.py \
  --source-backup /backups/aer-20260916-143000-a81d92f.db \
  --target /data/aer.db --dry-run

# 确认后执行
docker compose -f compose.yaml run --rm aer-runtime \
  python /app/scripts/restore_sqlite.py \
  --source-backup /backups/aer-20260916-143000-a81d92f.db \
  --target /data/aer.db --force
```

脚本保证：

- 先校验备份完整性，**通过之前不碰目标**（损坏的备份必须是空操作）；
- 目标已存在时必须 `--force`；
- 删除目标旁边的陈旧 `-wal` / `-shm`。这一步不能省：留下属于**旧数据库**的预写日志，会在下次打开时被重放到刚恢复的文件上，产生无声的数据损坏；
- 完成后再次校验目标完整性。

恢复之后：确认没有进程占用数据库，然后部署与该备份 `alembic_revision` 相符的镜像。

---

## 12. 排障（Troubleshooting）

| 现象 | 原因与处理 |
| --- | --- |
| `docker compose` 报 `AER_IMAGE must be set` | `.env` 里 `AER_IMAGE` 为空且 shell 也没导出。正常路径由 `deploy.sh` 导出；手工执行时请显式设置。 |
| 冒烟测试报 `directories.writable` 失败 | 宿主机目录属主不是 10001。执行第 4 节的 `chown`。 |
| 冒烟测试报 `database.revision` 不匹配 | 数据库版本与镜像期望不同。**镜像回滚后属正常现象**，见第 10 节。 |
| 冒烟测试报 `cannot read revision: ... alembic.ini` | 镜像里缺少 `alembic.ini` 或 `migrations/`。这是打包问题，请检查 `.dockerignore` 与 Dockerfile 的 `COPY`。 |
| 拉取镜像 `unauthorized` / `denied` | 服务器上那份短期凭据已过期（正常现象）。触发一次部署工作流即可刷新；只回滚则不受影响，见 6.4。 |
| 部署卡在 SSH | 检查 `PRODUCTION_KNOWN_HOSTS` 是否包含该主机的**当前**主机公钥（换过密钥会变化）。 |
| 备份 sidecar 显示 `git_sha: unknown` | 调用备份时没传 `--git-sha`。`deploy.sh` 一定会传。 |
| 想确认线上是哪个版本 | `cat /srv/aer/deploy/current.env` |

---

## 13. CI 与 CD 分别是什么

- **CI（Continuous Integration，持续集成）**：每次代码变更自动验证质量与兼容性。本项目在 Pull Request 上运行 `ruff` / `mypy` / `pytest`、全新数据库的 Alembic CLI 迁移、镜像构建、容器冒烟测试、compose 配置校验。**完全不接触生产**。
- **CD（Continuous Deployment，持续部署）**：`main` 通过质量门禁后，自动构建并发布**可追踪**的制品到目标环境。本项目会重新跑一遍质量门禁（合并后的 commit 与 PR 上测试的 commit 不是同一个），再构建镜像、推送、SSH 部署。

```text
PR ──► ci.yml ──► 质量门禁 + 构建 + 容器冒烟 + compose 校验
                      │
merge to main ────────┴──► deploy.yml
                              ├─ 质量门禁（重跑）
                              ├─ 构建并推送 sha-<commit>
                              └─ SSH → 备份 → 迁移 → 冒烟 → 写 current.env
```

---

## 14. 本轮已验证 / 未验证

诚实说明边界，避免有人误以为某条链路已经跑过。

| 项 | 状态 |
| --- | --- |
| `pytest` / `ruff` / `mypy` | ✅ 本机通过（Linux 上由 CI 复跑） |
| 备份 / 恢复 / 保留策略 | ✅ 自动化测试覆盖（含损坏备份、陈旧 WAL、幂等恢复） |
| Alembic 全新库 + schema parity | ✅ 通过 |
| `Dockerfile` 构建、容器冒烟 | ⚠️ **未在本机执行**：本机无 Docker Engine。由 `ci.yml` 在 `ubuntu-latest` 上执行 |
| `docker compose config` 校验 | ⚠️ 同上，由 CI 执行 |
| SSH 部署到真实服务器 | ⚠️ **未执行**：无生产凭据。链路已实现，首次真实部署请按第 7 节走一遍并核对本文档 |

---

## 15. 本轮明确不做的事

不做，是因为当前只有一台服务器，也因为本轮的目的是"能可靠地构建、验证、发布、迁移、备份和回滚"，而不是堆基础设施：

- Kubernetes / Helm / Terraform / Ansible
- Redis / Kafka / PostgreSQL
- 日志聚合（ELK / Loki）、指标（Prometheus / Grafana）
- FastAPI / Dashboard / HTTP healthcheck
- NeuG 知识索引、检索（Retrieval）、Embedding
- 数据库自动降级

未来需要时再引入，并各自记录架构决策。
