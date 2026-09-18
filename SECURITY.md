# 安全策略

AER 是一个**嵌入式运行时**：它不监听端口，没有 HTTP 端点，不是服务。这决定了它的
攻击面与 Web 应用不同——风险集中在**它替调用方持久化了什么**，以及**它能被谁读到**。
下面的范围按这两点写，不是通用清单。

---

## 支持的版本

| 版本 | 是否接收安全修复 |
| --- | --- |
| `0.6.x` | 是 |
| `< 0.6` | 否 —— 请先升级到 `0.6.x` |

0.x 阶段只维护 `main` 与最新一个 release。**本项目不承诺长期支持窗口**：目前没有任何
机制能保证旧分支持续收到修复，所以这里不写一个履行不了的政策。

---

## 报告漏洞

**请不要用公开 issue 报告漏洞。** 公开披露前请先给维护者修复窗口。

### 首选：GitHub Private Vulnerability Reporting

```text
仓库页 → Security → Report a vulnerability
```

> ⚠️ 截至本文写作时，本仓库的 Private Vulnerability Reporting **尚未开启**
> （GitHub API `enabled: false`），需要**仓库管理员**在
> Settings → Security → Private vulnerability reporting 中打开。
> 在它开启之前，请使用下面的降级流程。

### 降级流程（不依赖任何邮箱）

本仓库不公开安全邮箱，也不在文档里编造一个，所以走 issue 通道但**不带任何细节**：

1. 开一条 issue，标题写 `[security] private channel request`，正文只写涉及的组件
   （例如 `aer.storage` / `aer.knowledge` / 镜像 / CI）与你是否需要回执；
2. **不要**在 issue 里写复现步骤、不要贴日志、不要贴 trace、不要贴任何凭据；
3. 维护者建立私密渠道（开启 PVR，或另开一个只能维护者看到的渠道）后，把完整细节发到那里；
4. 如果一段时间没有回应，可以再开一条同样不含细节的 issue 提醒。

请不要把漏洞细节贴到任何公开位置——讨论区、PR 评论、社交媒体都算公开。

报告里请尽量给出：受影响的版本、组件与文件、最小复现、影响判断（读数据 / 写数据 /
凭据暴露 / 拒绝服务），以及你希望的处理时限。**不要附带真实的凭据**，用占位值即可。

---

## 安全范围

### 1. Trace 里的敏感信息

AER 的用途就是**存下真实执行轨迹**。因此以下都是设计上的敏感面：

- `events.payload`：`MODEL_CALL` 的 prompt、`TOOL_CALL` / `TOOL_RESULT` 的
  input 与 output，原样落库；
- `errors.error_message` 与 `stack_trace`；
- `verifications.message`（验证器是调用方提供的任意代码，可能把整页 HTML 或
  服务端响应当作解释塞进来）；
- `experiences` 的正文（problem / root_cause / solution / avoid / metadata）。

**结构化载荷是原样存储的**（见下面的"设计限制"）。如果你的 trace 里会有
客户数据、个人信息或凭据，请把它们挡在 AER 之外，或自行在写入前脱敏。

### 2. 凭据出现在 stack trace 或 repr 中

`aer/runtime/sanitization.py` 会按**形状**匹配并替换（PEM 私钥块、`Bearer`、
`authorization:`、`api_key/secret/token/password` 赋值、`sk-` / `AKIA` / `ghp_` 等前缀、
OpenSSH 密钥体、`scheme://user:password@host`），并截断超长文本。这些规则是保守的：
宁可漏掉，也不要改写正常的散文、路径与哈希。

### 3. SQLite 数据文件与目录权限

- 生产镜像以 **uid 10001** 运行，非 root；bind mount 的宿主机目录必须由同一 uid 拥有；
- **不加 `chmod 777`**。数据目录的权限就是数据库的权限，AER 自己不做访问控制；
- 库文件带 WAL 标记（`-wal` / `-shm` 边车文件）。备份、复制、只读挂载都要把它们一起考虑。

### 4. 备份文件

`scripts/backup_sqlite.py` 产出的是**明文 SQLite 文件 + JSON 边车**，没有加密。
备份目录（生产为 `/srv/aer/backups`）应与数据目录同等对待：

- 不进镜像、不进版本控制、不进构建上下文；
- 边车 JSON 含 `aer_version` / `alembic_revision` / `created_at` / `integrity_check`
  等元数据，**不含凭据**；如果你扩展了它，请保持这一点。

### 5. Distillation Provider

`DistillationProvider` 是**调用方提供的可调用对象**，在 AER 进程内执行。把不可信来源
接到这里等于把它的代码接进你的进程（它返回的 candidate 还要经过
`normalise_candidate` / `validate_candidate`，但那是**结构校验，不是沙箱**）。
`LLMVerifier` 同理。

### 6. 反序列化

JSON 载荷经 `aer/runtime/serialization.py` 的 `JsonValue` / `JsonObject` 契约进出，
编解码只发生在 `aer/storage/converters.py`。**AER 不 pickle 异常对象**，stack trace
存为纯文本。

### 7. 路径穿越

数据 / 制品 / 知识库 / 备份四个目录来自 `AER_*` 环境变量。用
`aer.config.load_deployment_config()` 加载会做校验（生产模式下要求绝对路径），
但**不阻止你把它指向任意目录**。恢复与演练脚本的目标路径由命令行参数给出
（`--source-backup` / `--target`），属于操作员输入——不要把它接到不受控的外部输入上。

### 8. 命令执行

AER 自身**不执行 shell**，也不 `eval`。执行外部命令的是：
`scripts/*.sh`（部署 / 回滚）、`Dockerfile` 的构建步骤，以及你自己写进 Verifier /
Provider 里的代码。

### 9. 依赖

运行时依赖：`alembic`、`pydantic`、`sqlalchemy`、`neug`（**精确锁定 `0.2.0`**，
见 `docs/DECISIONS.md` D-055）。知识层的正确性建立在 NeuG 0.2.0 的**实测行为**上，
升级前请先跑 `scripts/probe_neug_engine.py`。上游漏洞请报到对应上游项目。

### 10. 镜像与供应链

- 生产镜像只有一种标签形式 `ghcr.io/wike-chi/aer:sha-<commit>`，**不发 `latest`**；
- 服务器**不 clone、不构建**，只 `docker pull` 由 CI 构建并带有 commit 标签的镜像；
- 镜像构建期有一次网络请求：拉取 NeuG 全文扩展（Alibaba OSS）。这一步是刻意的，
  为的是让镜像自足；如果扩展源不可信，这一层就是供应链入口；
- 数据永远在 bind mount，**不进镜像**。

### 11. GitHub Actions 的秘密

- CI 与发布流程**不使用任何长期凭据**：PyPI 走 Trusted Publishing（OIDC），
  GHCR 用 job 自带的 `GITHUB_TOKEN` 并在 `pull` 之后 `docker logout ghcr.io`；
- SSH 私钥写成 `0600` 的文件而不是命令行参数，任何步骤都**不启用 `set -x`**；
- `permissions` 逐 workflow 最小化（例如发布 job 只有 `id-token: write` +
  `contents: read`）。

---

## 设计限制（请勿高估）

写清楚能力边界比宣称能力重要：

1. **Sanitizer 尚未完整实现。** 现在只有 `aer/runtime/sanitization.py` 那个最小实现，
   且**只作用于两处**：降级 fallback 的 `repr` 与 stack trace 文本。
   **调用方传入的结构化 payload（tool input/output、event payload、verification message）
   是原样落库的，不会被清洗。** 不要依赖 AER 替你过滤个人信息或凭据；完整的
   Sanitizer 是后续里程碑，届时才谈保留策略。
2. **静态数据不加密。** 数据库、备份、知识库索引都是明文。安全性等于文件权限。
3. **没有鉴权、没有多租户、没有审计日志。** 能读到文件的人就能读到全部 trace。
4. **不做出站网络请求**（除镜像构建期拉扩展）。检索完全本地。
5. **`verified_success` 不是安全结论。** 它表示"独立验证通过"，不表示"内容可信"或
   "数据已脱敏"。

---

## 相关文档

- [`CONTRIBUTING.md`](CONTRIBUTING.md) —— 开发、测试与迁移红线
- [`docs/DECISIONS.md`](docs/DECISIONS.md) —— 架构决策记录（含 D-038 镜像内不存数据、
  D-044 回滚不降级数据库）
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) —— 部署、权限与灾备
