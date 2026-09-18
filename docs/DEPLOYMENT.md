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
拉取完成后，工作流会**立刻把它从服务器上删掉**（`docker logout ghcr.io`，`if: always()`，
且在确认凭据确实消失之前不会静默通过）。

这条清理是 2026-09-17 的灾难恢复演练加上的：演练在一次例行检查中发现，上一次部署（数小时
前）的 `GITHUB_TOKEN` **还留在**服务器的 `/root/.docker/config.json` 里——一个 `ghs_` 前缀的
GitHub App 安装令牌，没人用它，也没人盯着它过期。它会自己失效，但在失效之前，它就是一台
公网可达主机上的常驻凭据。原设计假设"过期时间够短所以不用管"，演练证明这个假设不成立：
**只要没人删，它就一直躺在那儿。**

代价与应对：

- **自动部署永远可用**：每次部署都会重新登录一次。
- **服务器上手工执行 `deploy.sh`**：因为凭据多半已过期，拉取会失败。
  `deploy.sh` 会明确提示这一点（"凭据可能已过期，部署工作流会刷新它"）。
  需要手工部署时，先触发一次工作流，或在服务器上自行 `docker login ghcr.io`。
- **`rollback.sh` 不受影响**：回滚目标镜像通常已在本机，脚本在拉取失败时会
  自动退回本地副本（这正是事故现场最需要的行为），只在本地也没有时才报错。
- **凭据的存活窗口**：登录发生在部署步骤之前，登出发生在部署步骤之后，所以凭据只在
  「拉取镜像」这一段时间内存在。演练已实测该登出确实生效（见第 15 节）。

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
- 完成后再次校验目标完整性，并且**恢复目录里最终只剩数据库文件本身**（校验用的只读连接
  也会在文件旁留下 `-shm`，所以清理发生在最后一次打开之后——这条顺序由测试固定）。

### 11.1 从只读挂载恢复（灾难现场的常态）

**备份目录可以直接以只读方式挂载**，这是持有备份最安全的姿态（离线介质、快照、只读副本）：

```bash
docker run --rm --entrypoint python \
  -v /srv/aer/backups:/backups:ro \
  -v /srv/aer/restore-target:/data \
  "$AER_IMAGE" /app/scripts/restore_sqlite.py \
    --source-backup /backups/<backup>.db --target /data/aer.db --force
```

实现上有一处必须知道的细节：AER 的数据库**始终带着 WAL 标记**（模式写在文件头里），而
只读连接即使在 `mode=ro` 下也想创建 `-shm` 索引——在只读文件系统上会被内核拒绝，表现为
`unable to open database file`。因此校验会退回到 `immutable=1`（跳过锁与 WAL 机制）。

这个回退**只在没有非空 `-wal` 时才允许**。若文件旁存在非空的预写日志，脚本会**明确拒绝**
而不是拿主文件单独下结论——对陈旧页面给出一个自信的 "ok"，比不给答案更糟。

实现上，**所有读取路径都走同一个 `open_read_only()`**：完整性校验、sidecar 元数据，
以及**恢复时的源连接**。它用 `mode=ro` 打开后**强制读一次**（`PRAGMA schema_version`）：
`sqlite3.connect` 是惰性的，WAL 库在只读文件系统上不会在 connect 那一刻报错，而是等到真正
读取时才报——对恢复而言，那意味着在 `Connection.backup` 中途失败，并在目标位置留下一个
0 字节的 `aer.db`。

> 这条能力是 2026-09-17 演练补上的，而且**修了两轮**。第一轮只修了「校验」，「复制」仍然
> 以读写方式打开源，于是出现了一份自相矛盾的状态：`--dry-run` 从只读挂载返回 0，而真正的
> 恢复仍然 `unable to open database file`（exit 1）。**部署后验证在真实服务器上当场抓到
> 了这个组合**——一处回退存在两个调用点时，修一个不算修。现在有一条测试专门断言：
> 恢复的源**必须以只读方式打开**。

复制失败时也不会再留下一个 0 字节的 `aer.db`：若目标文件由本脚本创建，脚本会删掉它；
若目标原本就存在，则**绝不触碰**——`--force` 的语义是替换**内容**，不是删除文件。

---

### 11.2 恢复相关排障

| 现象 | 原因与处理 |
| --- | --- |
| `is not a readable SQLite database: unable to open database file` | 备份所在文件系统是只读的。当前版本会自动退回 `immutable=1`；若仍报错，请确认旁边没有**非空**的 `-wal`（有则脚本故意拒绝）。 |
| 恢复目录里出现 `aer.db-shm` / `aer.db-wal` | 不应发生。恢复脚本必须在最后一次打开之后清除它们，否则属于旧库的预写日志会被重放到新库上。这是测试固定住的不变量，若你看到了它，说明有不属于本项目工具的东西打开过该文件。 |

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

## 14. 真实部署记录与验证状态

### 首次部署（已完成）

```text
目标服务器 : bwg-06 (65.49.202.129, Linux x86_64)
镜像       : ghcr.io/wike-chi/aer:sha-f0215c4a2d1c3e5bf22a3f8b1595c1359e4b6492
数据库     : /srv/aer/data/aer.db（Alembic revision 0004，integrity_check = ok）
CI 运行    : actions/runs/35078175077（push 到 main 触发，全部 job 通过）
```

容器内对**生产库**的冒烟结果（`docker run ... smoke_test.py`）：

```text
[ok] runtime.version      aer 0.5.0, python 3.12.14
[ok] config.resolve       environment=production, db_path=/data/aer.db
[ok] directories.writable data=/data artifact=/artifacts knowledge=/knowledge backup=/backups
[ok] database.integrity   /data/aer.db -> ok
[ok] database.revision    0004 (head 0004)
[ok] runtime.open         runs=0 experiences=0
all 6 checks passed
```

### 状态表

| 项 | 状态 |
| --- | --- |
| `pytest` / `ruff` / `mypy` | ✅ 本机与 CI 均通过 |
| 备份 / 恢复 / 保留策略 | ✅ 自动化测试覆盖（含损坏备份、陈旧 WAL、幂等恢复） |
| Alembic 全新库 + schema parity | ✅ 通过 |
| `Dockerfile` 构建、容器端到端（建库→播种→备份→恢复→冒烟） | ✅ CI（ubuntu-latest）通过 |
| `docker compose config` 校验 | ✅ CI 通过（含"空 AER_IMAGE 必须被拒绝"的反向校验） |
| SSH 部署到真实服务器 | ✅ 已执行并验证（见上） |
| 镜像不含数据 | ✅ 实测 `/app/data` 不存在，生产库只在 bind mount |
| 镜像可回答"线上是哪个 commit" | ✅ `docker inspect` 的 `org.opencontainers.image.revision` = 该 commit |
| 备份分支（迁移前备份） | ⚠️ 首次部署时数据库尚不存在，按设计跳过；第二次部署起生效 |
| 回滚到上一版本 | ✅ 已实测（`rollback.sh`：dry-run + 真实回滚 + 滚回最新）。凭据已过期时自动退回本地镜像，仍在冒烟 6/6 通过 |
| 真实故障恢复演练 | ✅ 已完成（2026-09-17），见第 15 节 |
| 从只读挂载的备份恢复 | ✅ 已实测（演练发现的缺口，已修并复测） |

---

## 15. 灾难恢复演练（Disaster Recovery Drill）

演练日期：**2026-09-17**（UTC 02:52 – 03:31）。目的只有一个：证明生产备份不仅
「能生成」，而且**真的能在隔离目录恢复、迁移，并被当前线上镜像正确读取**。

### 15.1 演练记录

| 项 | 值 |
| --- | --- |
| 演练日期 | 2026-09-17 |
| 使用的备份 | `/srv/aer/backups/aer-20260916-093453-1c18b1e88801f982f4a8be229c23357e112e8c94.db`（+ `.json` sidecar） |
| 备份 sidecar 自述 | `aer_version=0.5.0`、`alembic_revision=0004`、`created_at=2026-09-16T09:34:53Z`、`integrity_check=ok`、`source_bytes=131072` |
| 使用的镜像 | `ghcr.io/wike-chi/aer:sha-1c18b1e88801f982f4a8be229c23357e112e8c94` |
| Source Revision | `0004`（备份自述，且实测一致） |
| Restored Revision | `0004`（恢复后实测；`alembic current` 与 `alembic heads` 均为 `0004 (head)`） |
| Integrity | 备份恢复前 = `ok`；恢复后目标 = `ok`；`PRAGMA integrity_check` 全程 `ok` |
| Smoke Result | **6/6 passed**，且 `config.resolve` 显示 `db_path=/data/aer.db`（演练挂载，非生产路径） |
| 是否影响 Production | **否**。数据库文件 sha256 与 mtime 演练前后完全一致，目录内始终只有 `aer.db` |
| 实际恢复耗时 | 备份校验 + 恢复到隔离目录 + 完整性复核 = **4 秒**（同机、单文件 128 KiB） |
| 迁移耗时 | 容器内 `alembic upgrade head`（已是 head）= **3 秒**，退出码 0，revision 前后均为 `0004 (head)` |

恢复目标：`/srv/aer/drill/{data,artifacts,knowledge,logs}`（独立目录，首次演练时不存在，
因此无需清理上一轮）。生产库 `aer.db` 全程**只读**。

### 15.2 验收链路（第 18 节要求，逐段真实执行）

```text
Production Backup
  ↓  备份存在 + sidecar 完整（不猜测缺失元数据）
Integrity Check            → ok
  ↓  scripts/restore_sqlite.py（禁止 cp 替代）
Isolated Restore           → /srv/aer/drill/data/aer.db，target_integrity = ok
  ↓
Restored Integrity Check   → ok
  ↓  当前线上 immutable image，容器内执行
Container Migration        → upgrade head 退出码 0（revision 已是 head，证明幂等）
  ↓  同一镜像，挂载 /srv/aer/drill/data
Container Smoke            → 6/6 passed
  ↓
Known Records Read Back    → 逐字段一致（见 15.4）
  ↓
PASS
```

生产库 `config.resolve` 与演练库的隔离性是被**断言**的，不是被假设的：演练脚本先比较
两者的 inode 与 sha256，不同才继续。

### 15.3 生产未被改动（第 12 节）

```text
mtime : 2026-09-16 02:15:39.469647021 -0700   （演练前后一致）
size  : 131072                                 （演练前后一致）
sha256: f1d72ef63dc824c8dc5629a657648becabd152e4480f89f35d706edd39214e87  （演练前后一致）
行数  : runs/events/errors/recoveries/verifications/experiences/experience_sources 全为 0
目录  : /srv/aer/data 内始终只有 aer.db
integrity_check: ok
```

**一处必须如实披露的副作用**：为搞清楚「为什么只读挂载下打不开」，演练做过一次
*可写*挂载探针，它在生产目录里造出了 `aer.db-shm`（32768 字节）与 `aer.db-wal`
（**0 字节**，即无任何未 checkpoint 的已提交数据）。两者已清除，数据库文件**逐字节未变**。

但 `/srv/aer/data` 的**目录 mtime 因此被改动**（→ 19:45）。这正是第 12 节提醒「不要用
mtime 作为唯一判断依据」的现实版本：数据库文件自己没动，目录的时间戳却变了。此后所有
只读检查都改用 `mode=ro&immutable=1`，它不会创建任何副文件。

### 15.4 记录保全（第 11 节）：为什么换了数据源

生产库当前是**空的**——8 张表齐全、revision `0004`、`integrity_check=ok`，但
`runs` / `events` / `errors` / `recoveries` / `verifications` / `experiences` /
`experience_sources` **全部 0 行**。它由 `alembic upgrade head` 创建，此后只被读过。

`0 == 0` 不能证明任何保全性质，所以演练在**沙箱内**用 AER 自己的公开 API 播种了一份
含真实记录的数据（`scripts/drill_seed.py`，贯穿标记 `drill-2026-09-17`），再走
「源 → 项目自带备份工具 → 项目自带恢复工具 → 逐字段回读」：

| 记录 | ID / 关键字段 |
| --- | --- |
| Run | `279c0f75-e886-470f-8239-fcfd78f31d93`，`SUCCESS`，`started_at=2026-09-17T03:07:15.299239` |
| Events | 9 条（`TASK_START`→`VERIFICATION`），`sequence` 合计 45 |
| Error | `e2988925-2799-448f-8cf0-290636055ace`，`builtins.ConnectionRefusedError`，`resolved=1` |
| Recovery | `8a3809d1-efb8-4d08-955e-17630247c72c`，`success=1` |
| Verification | `36081647-96c5-45ad-b88b-66d437f65325`，`http_status`，`passed=1`，`required=1` |
| Experience | `f0c81218-a081-47e7-a018-654d84dc4c56`，`kind=RECOVERY`，`status=VERIFIED` |
| ExperienceSource | (`f0c81218…`, `279c0f75…`) |

恢复后**逐字段读取全部一致**，且每张表的**内容摘要（SHA-256）也全部一致**
（`logical_equal: true`）。两个文件并非逐字节相同——只差 **3 个字节**，全部落在 SQLite
文件头的 change counter（offset 27，`2` vs `1`）。这正是不该用 checksum 当证据的原因：
checksum 会报假警，逻辑比较不会。

### 15.5 失败演练（第 14 节，仅在演练库上）

两轮，都在演练沙箱内，生产库未被挂载：

| 轮次 | 操作 | 结果 |
| --- | --- | --- |
| `sim`（有数据） | 备份后写入 marker run `72ef2287…`（runs 1→2，events 9→11）→ **删除**演练库 → 从备份重新恢复 | runs 回到 **1**、events 回到 **9**、marker **消失**、播种 ID 与经验 ID 原样保留 → 冒烟 **6/6**；恢复 3 秒 |
| `target`（生产备份） | 备份后写入 marker run `42693e21…`（runs 0→1）→ **删除**演练库 → 从备份重新恢复 | runs 回到 **0**、marker **消失** → 冒烟 **6/6**；恢复 2 秒 |

第二轮回答的是关键问题：**恢复回到的是备份的时点，而不是「文件还在」**。一个保留了备份
之后写入的「恢复」不是恢复——只有备份之后写进去的标记能暴露这个区别。

### 15.6 RPO 与 RTO（第 13 节）

**RPO（恢复点目标）**：目前**没有周期性备份**，备份只在部署时产生（迁移之前）。因此

> 能恢复到**最近一次成功部署之前的备份**。

换句话说，最后一次部署之后写入的数据不在任何备份里。当前生产库 0 行业务数据，实际风险
为零；一旦开始真实写入，这个窗口就变成真实的数据损失窗口。**这是本次演练最值得记住的
一条限制**：它不是 SLA，是机制现状。

**RTO（恢复时间目标）**：本次实际流程的基线——

| 阶段 | 本次耗时 |
| --- | --- |
| 备份完整性校验 + 恢复到隔离目录 + 恢复后复核 | 4 秒 |
| 容器内 `alembic upgrade head` | 3 秒 |
| 失败演练中的恢复（删库后重建） | 2–3 秒 |

这是**同机、同盘、单文件 128 KiB** 下的下界。真实 RTO 还要加上：发现事故、判断恢复点、
停止写入者、选择并部署匹配 revision 的镜像、人工确认。本轮**不制定 SLA**，只记录基线。

### 15.7 本轮发现并修复的缺陷

| # | 缺陷 | 后果 | 处理 |
| --- | --- | --- | --- |
| 1 | `restore_sqlite.py` 的 `main()` 在 `restore_backup()` 返回后又校验了一次目标 | 刚清掉的 `-shm` / `-wal` 被重新造出来，恢复目录里留下属于旧库的预写日志「同族」文件 | 已修：完整性结论由 `restore_backup` 返回，调用方不再重新打开目标。新增 CLI 层回归测试（旧代码上必失败） |
| 2 | 只读挂载下 `mode=ro` 打不开 WAL 库 | **无法从只读挂载校验或恢复备份**——最安全的备份持有方式恰好不可用 | 已修：退回 `immutable=1`，且**仅当旁边没有非空 `-wal`** 时才允许；否则明确拒绝。新增 4 项测试 |
| 3 | 部署流程把短期 `GITHUB_TOKEN` 留在服务器上 | 公网可达主机上常驻一个没人用的凭据 | 已修：工作流新增 `docker logout ghcr.io`（`if: always()`、在拉取之后、并校验确实移除） |
| 4 | `tests/storage/test_experience_repositories.py` 的排序断言依赖墙钟 | 测试在 2026-09-16 14:01 UTC 之后**自己开始失败**（预先存在，与本次改动无关） | 已修：改为显式时间戳，顺序确定 |
| 5 | **第一轮对缺陷 2 的修复不完整**：只修了「校验」，没修「复制」——恢复的源仍以读写方式打开 | `--dry-run` 从只读挂载通过，真正的恢复仍然失败。**这是部署后验证在真实服务器上抓到的**，不是测试抓到的 | 已修：`open_read_only()` 成为所有读取路径的唯一入口（含恢复的源），并在返回前强制读一次以暴露惰性失败；失败时不再留下 0 字节目标 |

缺陷 4 值得单独说明：它断言 `experience_sources.get_runs()` 的顺序，却让其中一行回退到
`utc_now()`，另一行硬编码 `BASE_TIME + 1 分钟`。当真实时钟越过那个时刻，顺序翻转，断言必然
失败——**生产代码是对的，测试在读时钟**。它在此前所有运行中都是绿的，然后毫无改动地变红。
门禁抓到它是设计使然（部署流水线会重跑门禁并因此拦下发布）。

### 15.8 复跑这次演练

三个检查工具已随镜像发布（`scripts/drill_facts.py`、`scripts/drill_compare.py`、
`scripts/drill_seed.py`），只读检查器**不可能写入**（只以 `mode=ro` / `immutable=1` 打开）。

```bash
# 0. 环境
IMAGE=$(sed -n 's/^AER_IMAGE=//p' /srv/aer/deploy/current.env)
DRILL=/srv/aer/drill
install -d -m 0750 -o 10001 -g 10001 "$DRILL"/{data,artifacts,knowledge,backups}
install -d -m 0755 "$DRILL"/logs

# 1. 选一份备份（不要自动挑最新：选恢复点是操作员的决定）
BK=/srv/aer/backups/<backup>.db
cat "$BK.json"            # 先读 sidecar，不猜测缺失元数据

# 2. 恢复前校验（只读挂载 + immutable，零写入）
docker run --rm --entrypoint python \
  -v /srv/aer/backups:/backups:ro -v "$DRILL":/tool:ro \
  "$IMAGE" /tool/drill_facts.py facts /backups/$(basename "$BK") --immutable

# 3. 恢复（只读挂载即可，见 11.1）
docker run --rm --entrypoint python \
  -v /srv/aer/backups:/backups:ro -v "$DRILL/data":/data \
  "$IMAGE" /app/scripts/restore_sqlite.py \
    --source-backup /backups/$(basename "$BK") --target /data/aer.db --force --json

# 4. 迁移（即使 revision 已是 head 也执行一次，证明幂等）
#    挂载点与镜像自带的 AER_* 变量一致，且绝不挂 /srv/aer/data
docker run --rm \
  -v "$DRILL/data":/data -v "$DRILL/artifacts":/artifacts \
  -v "$DRILL/knowledge":/knowledge -v "$DRILL/backups":/backups \
  "$IMAGE" alembic upgrade head

# 5. 冒烟（必须确认 config.resolve 的 db_path 指向演练目录）
docker run --rm \
  -v "$DRILL/data":/data -v "$DRILL/artifacts":/artifacts \
  -v "$DRILL/knowledge":/knowledge -v "$DRILL/backups":/backups \
  "$IMAGE" python /app/scripts/smoke_test.py
```

编排脚本（`drill_sim.sh` / `drill_verify.sh` / `drill_fail.sh`）位于
`/srv/aer/drill/`，属于站点专属运维脚本，未入库。

### 15.9 本次演练**没有**覆盖的

诚实列出边界，比让读者以为「演练过了就都安全」有用：

- **生产库 0 行**，所以记录保全只能在人工播种的数据上验证（15.4）。
- ~~**跨 revision 恢复未演练**~~：**已由第 16 节补上**（2026-09-17）。当时这条限制是真的：
  本次备份的 revision 恰好等于 head（`0004`），而真实灾难中要恢复的往往是一份**更旧**的备份。
- **同机同盘**：恢复目标与备份都在 `/srv`（同一块磁盘）。未验证跨机器、跨介质、从异地副本恢复。
- **没有周期性备份**，见 15.6 的 RPO。
- **未演练**：备份介质损坏/丢失、整机重建、`restore_sqlite.py` 在真实生产路径
  （`/srv/aer/data`）上的执行。
- `drill_seed.py` 的贯穿标记是固定字符串，重复演练需改常量。
- 镜像里 `scripts/*.py` **没有可执行位**，必须写成 `python /app/scripts/xxx.py`。
- **`smoke_test.py` 不能对只读挂载运行**（演练收尾时实测）：它按设计要检查
  `directories.writable`（在数据目录里写探针），并且 `AER(...)` 以读写方式打开库。
  只读挂载下它会报 `directories.writable` / `database.revision` / `runtime.open` 三项失败——
  这是**环境不匹配，不是数据库有问题**。只读姿态适用于**备份**（见 11.1），数据目录必须是
  可写的；恢复目标本来也必须是可写的。
  反过来值得记住的一对事实：**可写挂载 + 干净关闭不会留下 `-wal`/`-shm`**（实测生产目录
  在冒烟后仍然只有 `aer.db`），而**只读连接会留下它创建的那些副文件**。

### 15.10 修复的部署后验证（在真实服务器上复测）

修完不等于修好。三个缺陷各自随新镜像部署到 bwg-06 后，都用同一套脚本在**真实服务器**上
复测了一遍。**第二项第一次复测时失败了**，这一节因此也记录了「第一版修复不完整」这件事。

| 复测项 | 第一次（`sha-38f068b`） | 第二次（`sha-28ca341`） |
| --- | --- | --- |
| 服务器上是否残留 registry 凭据 | ✅ `{"auths": {}}` | ✅ `{"auths": {}}` |
| 演练工具是否随镜像发布 | ✅ 三个都在 `/app/scripts/` | ✅ |
| 只读挂载下**校验**备份 | ✅ dry-run exit 0 | ✅ dry-run exit 0 |
| 只读挂载下**真正恢复** | ❌ `exit 1: unable to open database file` | ✅ `exit 0, target_integrity=ok, 131072 字节` |
| 恢复目录是否只剩数据库 | ⚠️ 只剩一个 **0 字节** `aer.db`（失败残留） | ✅ 只有 131072 字节的 `aer.db` |
| 容器内 `alembic upgrade head` | ✅ exit 0 | ✅ exit 0 |
| 容器冒烟 | ✅ 6/6 | ✅ 6/6 |
| 生产库 sha256 / mtime | ✅ 未变 | ✅ 未变（`f1d72ef6…` / `2026-09-16 02:15:39 -0700`） |

第一次复测暴露的是**第一版修复只覆盖了两个调用点中的一个**：`integrity_check` 修好了，
`restore_backup` 打开**源**的那一处仍以读写方式打开。于是 `--dry-run` 通过而恢复失败——
一份自相矛盾的状态，单元测试看不见（测试里没有只读文件系统），只有把新镜像放到真实挂载
前才会现形。修法与教训见 D-047 的「事后补充」与 15.7 的缺陷 5。

那条 0 字节的 `aer.db` 也值得单独记一句：失败的恢复在目标位置留下了一个**看起来像数据库
的空文件**。第二次修复之后，由脚本创建的目标在失败时会被删除，而已存在的目标绝不被触碰。

第二次复测的全部输出保存在服务器 `/srv/aer/drill/logs/drill-postdeploy-20260917T035922Z.log`。

## 16. 跨版本恢复演练（Cross-Revision Recovery Drill）

演练日期：**2026-09-17**。这一轮只回答一个问题：

> 当生产只能恢复出一个**旧版本**的 SQLite 数据库时，当前版本的 AER 能否把它安全迁移到
> head，同时保留历史事实，并继续正常工作？

答案是 **YES**：23 项验收全部通过。所有操作在
`/srv/aer/drill-cross-revision/` 完成，生产库全程只读。

### 16.1 结果摘要

| 项 | 值 |
| --- | --- |
| 演练日期 | 2026-09-17 |
| Source Revision | `0003`（从**空库**用该旧版 Migration 构建，**没有**对任何现有库 `downgrade`） |
| Target Revision | `0004`（由 `alembic heads` 确定，代码里未硬编码） |
| Image | `ghcr.io/wike-chi/aer:sha-843e4a92bde8c8f28e18761d301721745fe5ec3a`（演练时的当前线上镜像） |
| 历史记录保全 | ✅ 2 runs / 11 events / 1 error / 1 recovery / 1 verification，**逐表行数、共有表 DDL、每表内容摘要、抽样行字段**全部一致 |
| 新 Schema 状态 | ✅ `experiences` 与 `experience_sources` 存在且**各 0 行**（迁移只建 schema，不造业务数据） |
| Runtime 读 | ✅ 打开成功（revision 0004），`verified_success=true`、error `resolved`、recovery `success`、verdict `passed` 全部读回 |
| Runtime 写 | ✅ 新建 run + 验证通过；经验管线产出 `SUCCESS/VERIFIED` 经验（1 条 source）；计数 3 runs / 1 experience |
| Smoke | ✅ 迁移**前** exit 1 且**未修改**该库；迁移**后** 6/6 通过，exit 0 |
| Production 影响 | ✅ 数据库 sha256 `f1d72ef6…` 与 mtime `1789550139` 演练前后一致 |
| 实际耗时 | **65 秒**（19 个步骤，含 11 次容器启动） |

### 16.2 演练链路（第 30 节验收，逐段真实执行）

```text
Revision 0003 DB（空库 + 旧版 Migration 构建）
  ↓  播种 0003 当时 Schema 能容纳的记录（版本感知，不用当前 Runtime）
Official Backup            → integrity ok，sidecar 记录 revision 0003
  ↓  删除源库（此后备份是唯一副本）
Official Restore（当前镜像）→ 仍是 0003，行数与源一致
  ↓
Current Image Migration    → alembic upgrade head，exit 0
  ↓
Revision current head      → 0004 == alembic heads
  ↓
Historical Data Preserved  → 行数 / DDL / 内容摘要 / 抽样字段 全部一致
  ↓
Current Runtime Reads      → 历史 run、错误已解决、恢复成功、验证通过
  ↓
Current Runtime Writes     → 新 run + 验证 + 经验（SUCCESS/VERIFIED）
  ↓
Experience Pipeline Works  → experience_sources 建立关联
  ↓
Smoke PASS                 → 6/6
  ↓
Restart（新进程）          → 3 runs（2 历史 + 1 新）、revision 0004、integrity ok
```

### 16.3 历史事实保全：比了什么

「备份恢复出来了」和「历史还在」是两件事。本轮四层都比：

1. **逐表行数**：迁移前后 `runs`/`events`/`errors`/`recoveries`/`verifications` 完全相同；
2. **共有表的 DDL**：每张表的 `CREATE TABLE` 语句与索引定义逐字比较
   （`drill_compare --shared-tables`，见 16.9 —— 这一层是本轮补上的，只比内容会漏掉列定义漂移）；
3. **每表内容摘要**：按 `rowid` 排序后的内容哈希，`logical_equal = true`；
4. **抽样行逐字段**：5 张表各取一行，**字段级**比较，`mismatches: none`。

`tables_only_in_a = []`——迁移没有丢掉任何一张表。两个文件当然不算逐字节相同（相差 44680
字节）：迁移本来就**应该**新增两张表。

### 16.4 新 Schema 的默认状态

`experiences` 与 `experience_sources` 在迁移后**存在且为空**。这一条单独验证，是因为一个
「迁移顺手造了几条假业务数据」的 bug，比「迁移丢数据」更难发现，也更难解释。

### 16.5 当前 Runtime 的读与写

读（历史）与写（现在）是两种不同的能力，分别验证：

- **读**：打开迁移后的库，两个历史 run 都能取到；`verified_success` 为真，错误 `resolved`
  为真，恢复 `success` 为真，验证 `passed` 为真——**关系**（哪条 recovery 修好了哪个 error、
  哪条 verification 属于哪个 run）都还在。
- **写**：新建一个 run、对它做验证、蒸馏出经验，得到 `SUCCESS/VERIFIED` 经验与 1 条
  `experience_sources`。旧数据与新数据**共存**（3 runs / 1 experience）。

### 16.6 冒烟测试在迁移前后的行为

这是本轮暴露缺陷的地方，因此单独记录：

| 时机 | 结果 |
| --- | --- |
| 迁移**前**（库在 0003） | `database.revision` 与 `runtime.open` 判失败，**exit 1**，并且**库仍然是 0003 / 6 张表**——没有被冒烟测试改掉 |
| 迁移**后** | 6/6 通过，`runtime.open: runs=3 experiences=1`，exit 0 |

迁移前那两条 FAIL 是**正确行为**：这个镜像期望 head，库不是，部署就不该被判定成功。而
「未修改该库」这一条是**修出来的**——见 16.9。

### 16.7 失败与可重复性

- **备份可重复使用**：同一份 0003 备份再恢复一次，仍是 0003 且行数与源一致。
- **不能写入的迁移失败且不留痕**：在**只读挂载**上执行 `alembic upgrade head`，exit 1，
  库仍然是 0003、`integrity_check` 仍为 `ok`。半迁移的数据库没有被当作可以「修一下继续」的东西。
- 演练副本当场删除，备份 sha256 全程不变。

### 16.8 对 RPO / RTO 的补充

上一轮（第 15.6 节）留下的最大缺口是「跨 revision 恢复未演练」。本轮把它补上：

- **RPO 不变**：备份仍然只在部署时产生（迁移之前）。现在多了一条底气——**迁移前那份备份
  是可用恢复点**，即使它比当前 head 旧。这正是 `deploy.sh` 备份早于迁移的意义。
- **RTO 的跨版本部分**：从「恢复旧备份」到「库在 head 且冒烟通过」= 恢复 + `upgrade head` +
  冒烟。本轮实测 65 秒完成 19 个步骤（含 11 次容器启动），其中迁移本身是秒级。
- **一条新的运维事实**：旧备份的 sidecar 里写着它自己的 `alembic_revision`。恢复一份旧备份后，
  你能从 sidecar 知道它是哪个版本，而不必去猜。

### 16.9 本轮发现并修复的缺陷

| # | 缺陷 | 后果 | 处理 |
| --- | --- | --- | --- |
| 1 | `smoke_test.py` 自称 strictly read-only，但会把**非 head 的库迁移到 head** | 恢复旧备份后，文档推荐的第一步排障命令会**在你看到 revision 之前把库改掉**，拿走唯一一次「迁移前确认版本」的机会 | 已修：revision 检查未通过时不尝试打开，并记为**未满足**而不是通过。新增 3 条回归测试，已验证在修复前失败（`the smoke test migrated the database`） |
| 2 | `drill_compare --shared-tables` 只比内容，不比 Schema | 一个只改列定义、不动数据的迁移会被判为「完全保全」 | 已修：同时比较每张表的 `CREATE TABLE` 与索引定义。新增 2 条测试（新增表不算差异、改列定义必须被抓到） |

缺陷 1 值得多写一句：它的**退出码一直是对的**（有失败即 1），文档承诺的也是对的，错的是
「只读」这半句承诺。一份自相矛盾的输出——前半句说「你这个库版本不对」，后半句把它改成对的
——比单纯报错更危险，因为它销毁了证据。

### 16.10 复跑这次演练

四个检查工具已随镜像发布（`scripts/drill_{facts,compare,seed_revision,runtime_probe}.py`）。
关键用法：

```bash
IMAGE=$(sed -n 's/^AER_IMAGE=//p' /srv/aer/deploy/current.env)

# 1. 从空库构建旧 revision（绝不 downgrade 现有库），目录属主必须是 10001
install -d -m 0750 -o 10001 -g 10001 /srv/aer/drill-cross-revision/source-0003/data
docker run --rm -v /srv/aer/drill-cross-revision/source-0003/data:/data "$IMAGE" alembic upgrade 0003

# 2. 版本感知地播种（会拒绝：revision 不符 / 表非空 / 列不存在）
docker run --rm --entrypoint python \
  -v /srv/aer/drill-cross-revision/source-0003/data:/data \
  "$IMAGE" /app/scripts/drill_seed_revision.py /data --revision 0003 --json

# 3. 正式备份 → 删源 → 正式恢复（见第 9、11 节）
# 4. 前向迁移
docker run --rm -v <restored-data>:/data "$IMAGE" alembic upgrade head

# 5. 只比共有表（DDL + 内容），并让当前 Runtime 读+写一次
docker run --rm --entrypoint python -v <backup-dir>:/backups:ro -v <restored-data>:/data:ro \
  "$IMAGE" /app/scripts/drill_compare.py /backups/<bk>.db /data/aer.db \
    --a-immutable --b-immutable --shared-tables
docker run --rm --entrypoint python -v <restored-data>:/data \
  "$IMAGE" /app/scripts/drill_runtime_probe.py /data --write --json
```

编排脚本（`xrev_drill.sh`）与判定器（`xrev_judge.py`）在
`/srv/aer/drill-cross-revision/`，属站点专属脚本，未入库。判定器只读证据文件、不重跑任何步骤
——否则一次偶然的成功会顶替实际记录下来的东西。

### 16.11 本次演练**没有**覆盖的

- **只验证了 0003 → 0004 这一条边**（按第 2 节要求）。0001/0002 起步的迁移路径由
  `tests/storage/test_migrations.py` 覆盖，但不是在真实服务器上用镜像跑的。
- **没有跨大版本**：`0003 → 0004` 是纯增量迁移（只加表），所以「迁移重写既有行」这一类风险
  没有被真正施压。未来若出现破坏性迁移，需要专门演练。
- **同机同盘**：备份、恢复目标仍在同一块磁盘上。
- **没有周期性备份**：RPO 仍等于「距最近一次成功部署」，见 15.6。
- 旧库由 Alembic 构建，**不是**从一份真实的旧生产备份恢复的——因为线上从未运行过 0003
  （生产库是部署当天直接建到 0004 的）。这一点无法用演练弥补，只能等真实历史积累。

---

## 18. 知识索引（NeuG）运维

M6 引入了一个可检索的图索引。**它不参与灾备**，这一点决定了下面每一条操作，
所以先说清楚：

```text
SQLite  = 唯一事实源
NeuG    = 从 SQLite 推导出来的投影，任何时刻都能重建

因此：知识库丢失 **不需要** 恢复 SQLite 备份，只需要一次 rebuild。
```

### 18.1 数据位置

```text
宿主机：/srv/aer/knowledge/aer-knowledge/      ← NeuG 数据库（一个目录）
        /srv/aer/knowledge/aer-knowledge.projection.json   ← 投影元数据（同级文件）
容器内：/knowledge/aer-knowledge
```

数据库路径来自 `AER_KNOWLEDGE_DIR`，**Python 代码里不出现 `/srv/aer`**（D-046）。

元数据文件记录 `projection_schema_version`。它是同级文件而不是库内节点，因为
NeuG 自己决定它拿到的路径是文件还是目录，而代码不应该依赖这一点。

目录里有什么：

```text
checkpoint/   runtime/   wal/   neugdb.lock
```

`neugdb.lock` 意味着**读写模式打开时独占**：`status` 与另一个正在写入的进程
不能同时打开同一个库。当前是"一次性容器"模型，所以不构成问题；
这也是将来考虑 Service Mode 的触发条件（D-061）。

### 18.2 状态

```bash
docker compose --file /srv/aer/deploy/compose.yaml run --rm   aer-runtime python -m aer.knowledge status
```

输出：

```text
knowledge database : /knowledge/aer-knowledge
reachable          : yes
projection schema  : 1
store experiences  : 0
index experiences  : 0
drift              : none
```

`drift` 是 SQLite 与索引在 `(id, updated_at)` 上的差集，分三类：

| 类别 | 含义 | 说明 |
| --- | --- | --- |
| `missing` | SQLite 有、索引没有 | 投影从未跑过，或跑失败过 |
| `stale` | 两边都有但 `updated_at` 不同 | 写入之后没有再投影 |
| `orphaned` | 索引有、SQLite 没有 | 记录被删了，投影还在 |

**`reachable: NO` 时会打印原因而不是抛异常。** 一个在你要诊断它时崩溃的
状态命令，等于什么都没告诉你。

### 18.3 增量补齐

```bash
... python -m aer.knowledge project
```

把 store 里还没投影（或已变化）的经验补上。每条记录是**替换**而不是追加，
所以对已有索引重复运行是安全的。日常用法是"提炼完一批经验之后跑一次"。

### 18.4 重建（万能的修法）

```bash
... python -m aer.knowledge rebuild
```

这四种情况都用它，不需要区分：

```text
知识库损坏 / 结构版本不符 / drift 不为 none / 目录被整个删掉
```

实现方式：在 `aer-knowledge.rebuilding` 建一份新的 → 用计数与指纹校验 →
**原子替换** → 删掉旧的。任一步失败，线上索引**毫发无损**；
`aer-knowledge.previous` 只是替换过程中的临时名。

重建只读 SQLite，从不写它。这是整个设计的要点：索引可丢弃，事实源不可。

一千条经验的重建约两秒（一次含写入的事务提交约 900ms，与语句数无关，
所以投影按批提交——D-063）。

### 18.5 知识库丢失的恢复

```bash
# 1. 确认丢了什么
docker compose ... run --rm aer-runtime python -m aer.knowledge status
#    → reachable: NO 或 index experiences 明显偏小

# 2. 重建（这就是全部恢复步骤）
docker compose ... run --rm aer-runtime python -m aer.knowledge rebuild

# 3. 确认
docker compose ... run --rm aer-runtime python -m aer.knowledge status
#    → drift: none
```

**不要去恢复 SQLite 备份。** store 没坏；从备份恢复会丢掉部署之后写入的经验，
而且修不了索引。

### 18.6 备份策略

知识库**不纳入关键备份资产**（§88）：它是可推导的，物理备份只能加速恢复，
不能替代 rebuild。`scripts/backup_sqlite.py` 只备份 SQLite，
**不需要**改成把 `knowledge/` 也打包。

代价要注意：`/srv/aer/knowledge` 目录本身还是要有人在，容器以 uid 10001 运行，
目录属主必须是它（见 §4「Ownership」）。

### 18.7 升级 `neug` 之前

```bash
... python /app/scripts/probe_neug_engine.py
```

它会逐条检查知识层依赖的引擎行为，**非零退出就不要升级**。这些行为大部分
没有写在文档里，有几条还与文档相反：

```text
文档说                             引擎实际做
--------------------------------  ------------------------------------------
execute() 接受分号分隔的多语句      拒绝（"We do not support preparing multiple
                                   statements in one query"）
CREATE INDEX [IF NOT EXISTS]       解析器直接拒绝（"Invalid input <NOT>"）
SHOW_NODE_TABLES() ...             "function SHOW_NODE_TABLES does not exist"
bm25 越小越相关                     还**是负值**；1/(1+bm25) 会把排序反转
（未提及）                          全文查询按 **FTS5 语法**解析：`wp-json 404`
                                   抛 "no such column: json"
（未提及）                          LIMIT $参数 被**静默忽略**
（未提及）                          重复 CREATE 同一条边会产生两条边
（未提及）                          STRING 等价于 VARCHAR(256)
（未提及）                          DROP TABLE 节点表会连带删关系表与全文索引
```

升级后重跑 `tests/knowledge/`（CI 在 ubuntu-latest 上真实安装 neug 并通过它跑
这些用例），再考虑改 `pyproject.toml` 里的锁定版本（D-055）。

### 18.8 明确不做

```text
向量检索（HNSW）/ embedding        等真实数据证明 BM25 不够用（D-056）
neuG 服务模式（db.serve()）         单机单进程下嵌入式更简单（D-061）
用 Alembic 管图 Schema            结构不符一律 rebuild（D-058）
跨库事务（2PC / Saga / outbox）    索引是可丢弃的副本（D-057）
检索时写 usage                     需要任务结果才能定义"有用"（D-060）
```

## 19. M6 生产验收记录（2026-09-18）

### 19.1 脚本

```bash
docker compose --file /srv/aer/deploy/compose.yaml run --rm \
  aer-runtime python /app/scripts/drill_knowledge.py /tmp/knowledge-drill
```

`/tmp` 而不是别的目录：compose 只挂 `/data` `/artifacts` `/knowledge` `/backups`，
在宿主某个自选目录上"准备好一个目录"对容器毫无意义（本轮在这上面错了两回）。

### 19.2 结果

```text
镜像          ghcr.io/wike-chi/aer:sha-39948667d6e86f3bcadfa6b44e19e7fe80c6c50f
知识库路径    /srv/aer/knowledge/aer-knowledge（容器内 /knowledge/aer-knowledge）
投影版本      1
store 经验数  0
索引 经验数   0
drift         none
BM25 检索     空库返回空结果（不是错误），渲染为 "No relevant experience found."
重启验证      第二个容器打开同一索引，状态与检索一致
drill         17/17 通过
```

drill 用一个临时库跑完整链路：两条经验（一条验证过的 RECOVERY、一条 FAILURE）→
投影 → 检索 → **删掉整个知识库** → 从 SQLite 重建 → 答案逐字段一致 → 再验证
"store 为空"这一真实生产状态。耗时：首次重建 1.9–3.0 秒（含全文索引创建），
重建一份已存在的索引约 0.6–1.9 秒。

### 19.3 生产数据库未被改动

```text
sha256 : f1d72ef63dc824c8dc5629a6527... （演练前后一致）
mtime  : 2026-09-16 02:15:39 -0700      （演练前后一致）
目录   : /srv/aer/data 内只有 aer.db
凭据   : /root/.docker/config.json 仍是 {"auths": {}}
```

### 19.4 本轮在生产验收里发现并修掉的问题

三次 drill 运行、三次失败，每一次都是**本机看不见**的：

1. **rebuild 残留暂存元数据**：swap 只改目录名，`aer-knowledge.rebuilding
   .projection.json` 留在知识目录里。
2. **修 1 的方式是错的**：事后删除暂存产物，删掉了本该**改名就位**的边车 →
   重建后线上索引没有元数据 → 下一次打开被 `ensure_schema` 正确拒绝。
   把 swap 抽成 `_swap_index_artifacts()` 并直接测它（5 条）。
3. **验收脚本的路径不在挂载里**：宿主根目录下建的目录容器看不到；
   同时 drill 的参数语义从"必须已存在"改为"能创建或可写"，并在不能时给出
   可执行的提示。

第 2 条值得单独记住：**"修好了"必须由目标环境确认**，因为修 1 的补丁在本机
测试全绿。

### 19.5 验收之后的线上状态

```text
/srv/aer/knowledge/
├── aer-knowledge/                     NeuG 数据库（76K，空库）
└── aer-knowledge.projection.json      {"projection_schema_version": 1, ...}
```

生产 store 为空，所以索引也为空——这是**合法状态**，不是失败。
有数据的完整投影与检索由 drill 在临时库上验证（`§19.2`）。

## 17. 本轮明确不做的事

不做，是因为当前只有一台服务器，也因为本轮的目的是"能可靠地构建、验证、发布、迁移、备份和回滚"，而不是堆基础设施：

- Kubernetes / Helm / Terraform / Ansible
- Redis / Kafka / PostgreSQL
- 日志聚合（ELK / Loki）、指标（Prometheus / Grafana）
- FastAPI / Dashboard / HTTP healthcheck
- 向量检索（HNSW）与 embedding（BM25 + 图过滤已落地，见第 18 节；向量留给 M6.5）
- 记录 `reuse_count` / `success_rate` 等使用统计（属 M7）
- 数据库自动降级

未来需要时再引入，并各自记录架构决策。

---

## 20. 发布到 PyPI（Release / Trusted Publishing）

### 发布是什么形状

Tag 驱动，不手工上传：

```text
main 全绿 → 版本号一致（pyproject.version == aer.__version__）
         → 打 tag vX.Y.Z → 创建 GitHub Release（published）
         → .github/workflows/publish.yml：build → twine check → 干净 venv 里装 wheel 建库
         → PyPI Trusted Publishing（OIDC）上传
```

`publish.yml` 只有一个 job，这是刻意的：**上传的字节就是被验证的字节**。拆成两个 job
就要经 `upload-artifact` / `download-artifact` 转手一遍，那时"PyPI 上那份"与"验证过的那份"
之间就只靠约定，而不是靠结构。

仓库里**没有也不会有任何 PyPI 凭据**：上传用的是 PyPI Trusted Publishing——job 向 GitHub
要一个短期 OIDC token（`id-token: write`），PyPI 之所以接受它，是因为 publisher
（仓库 + workflow + environment）已经在 PyPI 网站上登记过一次。所以 workflow 顶层
`permissions: {}`，上传 job 只要两个 scope。

### 一次性人工配置（仓库端做不到）

这几步**只能人工在网站上做**，代码仓库里无法完成：

1. **PyPI 上登记 Trusted Publisher。**
   登录 PyPI → `Your projects` → `Publishing` → `Add a pending publisher`，填：

   | 字段 | 值 |
   | --- | --- |
   | PyPI Project Name | `aer-runtime` |
   | Owner | `Wike-CHI` |
   | Repository name | `aer` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   四个值必须与 `publish.yml` **逐字**一致。`environment` 名字对不上时的表现是
   OIDC 交换被拒，而不是"少了一个设置"，所以先把名字抄对再排障。

2. **（可选，但建议）给 `pypi` environment 加保护。**
   GitHub → Settings → Environments → 新建 `pypi` → 加 required reviewers。
   加了之后，publish job 会停下来等人批准；不加则 Release 一发布就直接上传。
   加与不加都不需要改 `publish.yml`——这正是用 environment 而不是用变量的原因。

3. **开启 Private Vulnerability Reporting。**
   Settings → Security → Private vulnerability reporting。
   `SECURITY.md` 把它列为首选渠道，但它的当前状态是 **未开启**
   （GitHub API `enabled: false`）。

`aer-runtime` 这个项目名在 PyPI 上**当前未被占用**（2026-09-18 查询返回 404），
所以不需要改名。**如果将来发现被占用，不要自行换名**——换名会让所有已经发布的文档、
badge 与安装说明同时失效，那是个需要单独决策的动作。

### 第一次发布的顺序（建议）

在动 PyPI 之前，先用 dry run 把仓库端这条链路跑通：

```text
GitHub → Actions → publish → Run workflow
    tag     = v0.6.0
    dry_run = true
```

`dry_run` 会执行**除上传以外的全部步骤**：解析 tag、校验 tag 与版本一致、`python -m build`
（sdist → 从 sdist 出 wheel）、`twine check --strict`、把 wheel 装进一个干净 venv 并真的
建一个库、读回 revision。这一步能把"制品里没有迁移脚本"这类问题挡在 PyPI 之外——
而它是真实发生过的故障（见 `docs/DECISIONS.md` D-064）。

dry run 绿了之后，再创建 Release 触发真正的上传。

### 失败与重试

- **PyPI 同一版本号不可重传。** 传错或传漏只能换版本号（或联系 PyPI 支持删除，
  但那通常是几个工作日）。
- 如果失败发生在上传**之前**，直接 `Run workflow` 重试，不用动 Release。
- 如果失败发生在上传**之中/之后**，先看 PyPI 上的文件列表：可能已有部分文件。
  此时不要重复上传同一个版本号。
- `workflow_dispatch` 的 `tag` 输入必须填成 `vX.Y.Z`，且等于 `pyproject.toml` 的
  `version`；不一致时 workflow 会在构建之前就失败。

### 发布后确认

```bash
pip install --upgrade aer-runtime
python -c "import aer; print(aer.__version__)"
python -c "from aer import AER; AER('/tmp/aer-release-check').close()"   # 构造即迁移
```

第三条是真正的验收：它能过，说明 wheel 里的包自带迁移脚本，`pip install` 是一条自足的路径。

**再加一步，且在 Windows 上做**（v0.6.1 的教训）：**复核依赖能否解析**。
CI 跑在 Linux 上，永远发现不了"某个依赖没有 Windows 分发"这类问题——

```bash
pip install --dry-run --ignore-installed aer-runtime
```

v0.6.0 就是栽在这里：`neug==0.2.0` 只有 macOS 与 Linux 的 wheel，而它是必需依赖，
于是 Windows 上整条安装直接失败（`docs/DECISIONS.md` D-065）。发布后顺手跑一次这条命令，
比等用户来报更便宜。

如果之后要复查引擎本身的可用性：

```bash
pip install --upgrade "aer-runtime[knowledge]"
python -c "import neug; print(neug.__file__)"
```
