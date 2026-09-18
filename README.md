# AER — Agent Experience Runtime

> 智能体经验运行时

AER 是一个面向 Agent 系统的**轻量级经验学习基础设施**。

它的目标不是构建通用 Agent，也不是让模型实时修改自身权重，而是：

> 将 Agent 在真实任务中的执行、失败、恢复、验证和成功过程，
> 转化为可追踪、可验证、可检索、可复用的经验。

核心闭环：

```text
Task → Agent Execution → Hook Capture → Trajectory → Verification
     → Experience Distillation → Experience Store → Retrieval → Next Agent Execution
```

第一阶段重点是 **Experience Retrieval**，而不是 **Model Training**。

[![deploy](https://github.com/Wike-CHI/aer/actions/workflows/deploy.yml/badge.svg)](https://github.com/Wike-CHI/aer/actions/workflows/deploy.yml)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://github.com/astral-sh/ruff)

> 流水线徽章指向 `deploy.yml`：`ci.yml` 只在 PR 上运行，而 `deploy.yml` 在 `main`
> 上重跑同一套质量门禁，所以它是"当前主线是否绿"的那个信号。

---

## 安装

要求 **Python >= 3.12**。

从源码（当前推荐；见下方说明）：

```bash
git clone https://github.com/Wike-CHI/aer.git
cd aer
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

发行版（`pip install aer-runtime`）通道已经配置好：PyPI 用 Trusted Publishing，
由 GitHub Release 触发 [`.github/workflows/publish.yml`](.github/workflows/publish.yml)。
**在 `v0.6.0` 的 release 真正发布到 PyPI 之前，请不要把下面这行当成已经可用**：

```bash
pip install aer-runtime    # 仅在首次 release 成功发布到 PyPI 之后成立
```

开发依赖：pydantic 2.x、SQLAlchemy 2.x、Alembic 1.13+、NeuG 0.2.0；
`[dev]` 额外带 pytest / ruff / mypy / pyyaml。

---

## 当前状态

```text
Milestone 1 — Runtime Core           ✅
Milestone 2 — SQLite Storage         ✅
Milestone 3 — Hook & Trace           ✅
Milestone 4 — Verification           ✅
Milestone 5 — Experience Core        ✅
Infrastructure — Repo / Docker / CI / CD / Backup  ✅
```

已实现：

- 统一枚举：`RunStatus` / `EventType` / `VerifierType`（按信任度声明）/
  `ExperienceKind` / `ExperienceStatus` / `DistillationTrigger`
- 领域模型：`Run` / `Event` / `ErrorRecord` / `RecoveryRecord` /
  `VerificationRecord` / `Experience` / `ExperienceSource`
- SQLite 存储：WAL + 外键 + `synchronous=NORMAL` + `busy_timeout=5000`
- 八张表：`runs` / `events` / `errors` / `recoveries` / `verifications` /
  `experiences` / `experience_sources`（+ `alembic_version`）
- **Schema 由 Alembic 管理**（`alembic upgrade head`，revision 0001–0004），
  不再依赖 `create_all`
- Repository 层：`Run` / `Event` / `Error` / `Recovery` / `Verification` /
  `Experience` / `ExperienceSource`
- 嵌入式运行时：`AER` + `RunContext`
- **Hook 上下文管理器**：`run.tool()` / `run.recovery()`，异常路径也保证写出结束事件
- 统一 Error 管道：`run.error()`，结构化落库 + 脱敏后的 stack trace
- **独立验证引擎**：`run.verify()` → `VERIFICATION` 事件 + `VerificationRecord`
- 四类验证器：`DETERMINISTIC`（`HttpStatusVerifier` / `H1CountVerifier` /
  `JsonValidVerifier` / `PredicateVerifier`）、`ENVIRONMENT`、`HUMAN`、`LLM`
- **`verified_success`**：区分「Agent 说完成」与「独立确认完成」
- **经验提炼**：`run.distill()` → `Experience` + `ExperienceSource`，
  三类经验 `SUCCESS` / `RECOVERY` / `FAILURE`，生命周期受状态机约束
- Event `sequence` 严格递增，自动写入 `TASK_START` / `TASK_END`
- **可部署制品**：`Dockerfile`（非 root、可丢弃、OCI 标签带 commit）、
  `deploy/compose.yaml`、`scripts/{deploy,rollback,smoke_test}.sh`、
  `scripts/{backup,restore}_sqlite.py`、`.github/workflows/{ci,deploy}.yml`
- **灾备可验证**：备份/恢复工具、`scripts/drill_{facts,compare,seed}.py`
  与已真实执行过的[灾难恢复演练](docs/DEPLOYMENT.md#15-灾难恢复演练disaster-recovery-drill)

尚未实现（后续 Milestone）：Retrieval（FTS5 / Embedding）、Experience Injection、
Experience Usage、Workflow、Dataset、完整 Sanitizer、Artifact、CLI、FastAPI、
Dashboard、模型训练。

本轮**刻意不做**（见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) 第 16 节）：
Kubernetes / Helm / Terraform / Ansible、Redis / Kafka / PostgreSQL、
日志聚合与指标系统、HTTP healthcheck、NeuG。

---

## 快速开始

```python
from aer import AER, EventType

aer = AER("./data")          # 自动建目录、跑迁移、打开 SQLite，无需任何服务

run = aer.start_run(
    task="Fix WordPress product page H1",
    task_type="wordpress",
    agent_name="wp-agent",
)

# 1. 工具调用：进入写 TOOL_CALL，退出写 TOOL_RESULT
with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
    tool.set_result({"status": 200})

# 2. 失败会被自动记录为 ERROR + ErrorRecord，异常照常抛出
attempt = run.tool("wordpress.update_page", input={"page_id": 123})
try:
    with attempt:
        raise PermissionError("403 Forbidden")
except PermissionError:
    pass

# 3. 恢复：显式关联到刚才那个错误，成功后自动 resolve
error = attempt.error_record
assert error is not None

with run.recovery(reason="REST API returned 403", error_id=error.id) as recovery:
    recovery.set_result({"action": "granted_edit_posts"})

# 4. 重试成功
with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
    tool.set_result({"status": 200})

# 5. Agent 声明完成 —— 这只是 Agent 的自述，还不是验证结论
run.success(final_score=1.0)
aer.close()
```

产生的 Trace：

```text
1  TASK_START
2  TOOL_CALL          wordpress.update_page
3  TOOL_RESULT        success
4  TOOL_CALL          wordpress.update_page
5  ERROR              builtins.PermissionError: 403 Forbidden
6  TOOL_RESULT        success=false
7  RECOVERY_START     reason="REST API returned 403"
8  RECOVERY_RESULT    success=true
9  TOOL_CALL
10 TOOL_RESULT        success
11 TASK_END
```

---

## 验证：Agent 说的成功 ≠ 验证的成功

```python
from aer import H1CountVerifier, HttpStatusVerifier, VerificationContext

run = aer.start_run(task="Fix the H1", task_type="wordpress")
with run.tool("wordpress.update_page", input={"page_id": 123}) as tool:
    tool.set_result({"status": 200})
run.success()                       # Run.status = SUCCESS

# 验证通常发生在 Agent 结束之后，甚至可以由另一个进程按 run_id 补做
ctx = VerificationContext(
    run_id=run.run_id,
    payload={"actual_status": 200, "actual_count": 0},   # 真实观测到的证据
)

run.verify(HttpStatusVerifier(expected_status=200), context=ctx)   # PASS
run.verify(H1CountVerifier(expected_count=1),      context=ctx)   # FAIL

run.status                  # SUCCESS  ← 执行事实，不会被改写
run.get_verification_summary()
# VerificationSummary(total=2, passed=1, failed=1, pass_rate=0.5, all_passed=False, ...)
run.verified_success()      # False    ← 派生判断
```

需要同时保存的两个事实：

```text
Agent execution:   SUCCESS     # Agent 做完了它认为该做的事
Verification:      FAILED      # 真实环境没有满足最终要求
Verified success:  FALSE
```

`verified_success` 的定义（唯一实现见 `aer/verification/summary.py`）：

```text
verified_success := Run.status is SUCCESS
                    and required_total > 0        # 「没有验证」不等于「全部通过」
                    and required_failed == 0
```

---

## 经验：从执行轨迹到可复用知识

```python
from aer import AER, CallableDistillationProvider, ExperienceCandidate

provider = CallableDistillationProvider(
    name="my-provider",                 # 可以是规则函数，也可以是模型适配器
    func=lambda evidence: ExperienceCandidate(
        domain="wordpress",
        title="REST API 403 on page update",
        problem="Updating a page returns 403 and the H1 never changes",
        failed_attempts=("blind retry",),
        root_cause="missing edit_posts capability",
        solution="switch to a credential with edit_posts",
        avoid=("retrying without checking permissions",),
    ),
)

with AER("./data", distillation_provider=provider) as aer:
    # ... 上面那个 Run：失败 → 恢复 → 成功 → 验证通过 ...
    experience = run.distill()

    experience.kind              # RECOVERY  ← 由运行事实决定，不是 Provider 说了算
    experience.status            # VERIFIED  ← 轨迹已提炼，且结果有证据
    experience.failed_attempts   # ('blind retry', ...)  失败尝试与修复同样重要
    experience.solution
    experience.outcome_verified  # True
    aer.experience_sources.get_runs(experience.id)   # 这条知识来自哪个 Run
```

三类经验（`ExperienceKind`）：

| Kind | 何时产生 | 它回答什么 |
| --- | --- | --- |
| `SUCCESS` | 任务完成并经独立确认，且没有修复过程 | 这样做可行 |
| `RECOVERY` | 出现失败 → 修复 → 最终仍确认成功 | 踩了什么坑、怎么修的 |
| `FAILURE` | Run 失败，**或** Agent 说成功而验证否定 | 历史上哪些做法**没有**解决问题 |

关键规则：

- **失败也会被提炼。** `verified_success == false` 不是丢弃的理由 ——
  Agent 误判成功是最有价值的信号之一。
- **`FAILURE` 不承载 `solution`。** Provider 若给了方案，会被移入 metadata 作为
  假设；失败记录绝不会把未验证的猜测当作答案。
- **`kind` 由系统按运行事实判定**，Provider 的建议会被记录并忽略。
- **提炼幂等。** 同一 Run 重复 `distill()` 返回同一条经验；不同 Run 的相同 claim
  会合并为一个来源。重复出现让知识更强，而不是让库更大。

生命周期（`aer/runtime/lifecycle.py` 是唯一规则来源）：

```text
RAW ──► DISTILLED ──► VERIFIED ──► REUSED ──► PROVEN ──► TRAINING_CANDIDATE ──► TRAINING_DATA
 │          │            │
 └──────────┴────────────┴──────────► DEPRECATED（终态）

当前只有前三个状态可达：
  REUSED 及以上需要 experience_usage 统计（M7），本轮不存在任何到达它们的转换
```

---

## 项目结构

```text
aer/
├── exceptions.py             # 异常层次（Verification / Experience / 配置）
├── config.py                 # 部署配置：AER_* 环境变量（不引入 Settings 框架）
├── runtime/
│   ├── enums.py              # RunStatus / EventType / VerifierType / ExperienceKind / ...
│   ├── lifecycle.py          # Experience 状态转换表（唯一规则来源）
│   ├── models.py             # 领域模型（Pydantic；Experience 为 frozen）
│   ├── serialization.py      # JSON 类型契约 + 降级 marker
│   ├── sanitization.py       # 最小脱敏与截断
│   ├── hooks.py              # ToolContext / RecoveryContext
│   ├── run.py                # RunContext（状态机 / Error 管道 / verify / distill）
│   └── runtime.py            # AER 外观类（嵌入式入口 + 装配）
├── verification/             # M4：独立验证
│   ├── base.py / deterministic.py / environment.py / human.py / llm.py
│   ├── engine.py             # 唯一验证管道
│   └── summary.py            # 汇总 + verified_success（派生）
├── experience/               # M5：经验核心
│   ├── evidence.py           # RunEvidence + RunEvidenceBuilder
│   ├── classify.py           # 由运行事实判定 kind（纯函数）
│   ├── policy.py             # DistillationPolicy（值不值得提炼）
│   ├── candidate.py          # ExperienceCandidate + 结构校验
│   ├── dedup.py              # 确定性去重指纹
│   ├── provider.py           # DistillationProvider 协议（不绑厂商）
│   ├── distiller.py          # 提炼段（调用 Provider + 规范化）
│   └── service.py            # ExperienceService（唯一提炼管道）
├── storage/
│   ├── connection.py / database.py / migrations.py / converters.py
│   ├── models.py             # 七张业务表
│   └── repositories/         # 每张表的仓储
└── schemas/                  # 预留：未来 FastAPI 的传输层 Schema

deploy/
├── compose.yaml              # 单服务、四挂载；用 `run --rm` 执行一次性命令
└── env.example               # 环境变量模板（唯一入库的环境文件，不含任何凭据）

scripts/                      # 运维入口点（按文件执行，不是可导入包）
├── backup_sqlite.py          # SQLite 在线备份 API + 完整性校验 + sidecar 元数据
├── restore_sqlite.py         # 显式恢复（必须给 --source-backup/--target）
├── smoke_test.py             # 生产冒烟：只读校验 + 临时库写路径校验
├── smoke_test.sh             # 上面脚本的薄封装
├── deploy.sh                 # 备份 → 迁移 → 冒烟 → 记录 current.env
├── rollback.sh               # 只切镜像；不自动降级数据库
├── drill_facts.py            # 只读：事实/行数/完整性/代表性记录（演练用，不可能写入）
├── drill_compare.py          # 字节差异定位 + 逻辑等价比较；--shared-tables 用于跨版本
├── drill_seed.py             # 在演练沙箱内播种每类记录各一条（演练用）
├── drill_seed_revision.py    # 版本感知播种：旧 revision 的库绝不用当前 Runtime 写
└── drill_runtime_probe.py    # 当前 Runtime 读+写探测；默认拒绝打开非 head 的库

.github/workflows/
├── ci.yml                    # PR：质量门禁 + 全新库迁移 + 镜像构建 + 容器冒烟
├── deploy.yml                # main：重跑门禁 → 推送 sha 镜像 → SSH 部署
└── publish.yml               # GitHub Release → 构建并发布到 PyPI（Trusted Publishing / OIDC）

Dockerfile / .dockerignore    # 运行镜像；数据绝不进镜像
setup.py / MANIFEST.in        # 打包 shim：构建期把 alembic.ini + migrations/ 复制进 wheel（D-064）
LICENSE / CONTRIBUTING.md / SECURITY.md

migrations/versions/          # 唯一真源；wheel 内的 aer/_migrations/ 由 setup.py 在构建期复制
├── 0001_baseline_runs_and_events.py
├── 0002_errors_and_recoveries.py
├── 0003_verifications.py
└── 0004_experiences_and_sources.py

tests/
├── runtime/                  # 领域模型 / 生命周期 / 事件 / 三个 Hook
├── verification/             # 原语 / 协议 / 引擎 / 崩溃语义 / 终态守卫 / 汇总
├── experience/               # 模型 / 状态机 / 策略 / 证据 / 提炼 / 去重 / 来源幂等
├── storage/                  # 初始化 / 迁移 / 仓储 / 持久化
├── infra/                    # 部署配置 / 备份恢复 / compose / Dockerfile / workflow
└── integration/              # M3 失败-恢复 / M4 Agent-Verifier 冲突 / M5 三类经验

data/
└── aer.db                    # 默认数据库（不纳入版本控制）
```

分层原则：

```text
Business Agent
     ↓
AER SDK (AER / RunContext / ToolContext / RecoveryContext)
     ↓
Experience (Evidence / Policy / Provider / Service)     Verification (Verifier / Engine)
     ↓                                                         ↓
Repository (Run / Event / Error / Recovery / Verification / Experience / ExperienceSource)
     ↓
SQLAlchemy ORM
     ↓
SQLite
```

- `aer.runtime` 是领域层，**不 import** `experience` / `verification` / 任何存储细节；
- `AER` 是装配根：它是唯一同时认识所有层的地方。因此
  `aer.runtime` **不重导出 `AER`**（那会让 `import aer.runtime.enums` 拖入整个
  应用图并形成真实循环）——用 `from aer import AER`；
- 业务代码**不得**直接执行 SQL，也不得直接使用 `Session`。

---

## 数据库迁移

Schema 由 Alembic 管理，`Base.metadata` 只是 autogenerate 的输入。

```bash
# 升级到最新
alembic upgrade head

# 指定数据库（默认 ./data/aer.db）
alembic -x db_path=/tmp/other.db upgrade head
AER_DB_PATH=/tmp/other.db alembic upgrade head

# 查看当前版本
alembic current
```

`alembic` 默认在当前目录找 `alembic.ini`，所以上面这些命令面向**源码检出**与
**镜像内**（`/app` 下 `alembic.ini`、`migrations/`、`aer/` 三件套齐全）。

从 wheel 安装的包没有仓库根目录：迁移脚本在包内 `aer/_migrations/`（构建期由
`setup.py` 复制，见 `docs/DECISIONS.md` D-064）。两种等价写法：

```bash
# 1) 直接用包内配置
alembic -c "$(python -c 'import aer,pathlib; print(pathlib.Path(aer.__file__).parent/"_migrations/alembic.ini")')" upgrade head

# 2) 不用 CLI：AER(...) 在构造时就会把库迁移到 head
python -c "from aer import AER; AER('./data').close()"
```

规则：

- **修改 Schema 必须新增 revision**，禁止手工改库；
- 新增自定义列类型时，需在 `migrations/env.py` 的 `render_item` 中补充映射，
  保证 revision 不引用应用代码；
- `alembic revision --autogenerate` 会自动执行 `ruff format` + `ruff check --fix`；
- Schema 与 ORM 的一致性由 `tests/storage/test_migrations.py` 的 parity 测试固定。

---

## 开发

依赖：Python 3.12+、pydantic 2.x、SQLAlchemy 2.x、Alembic 1.13+。
开发额外需要 pytest / ruff / mypy / pyyaml（最后一个用于实际解析 compose 与
workflow 文件，而不是对它们做文本匹配）。

```bash
pip install -e ".[dev]"

pytest
ruff check .
ruff format --check .
mypy aer/
```

基础设施部分的额外校验（有 Docker 时）：

```bash
docker build -t aer:local .
docker run --rm aer:local                       # 打印版本后立即退出
docker compose -f deploy/compose.yaml config    # 需要 deploy/.env
```

> 提炼的 Provider 不配置时 `distill_run` 会大声报错；验证与提炼的流水线本身
> 完全离线可测，测试套件不访问网络。



---

## 检索：SQLite 是事实源，NeuG 只是它的投影

M6 引入了一个可检索的知识索引，用的是嵌入式图数据库 NeuG。**一句话的规则**：

```text
SQLite  = 唯一事实源（runs / events / errors / recoveries / verifications
          / experiences / experience_sources）
NeuG    = 从 SQLite 推导出来的可重建投影，只用来检索
```

没有任何一行代码把图库里的东西写回 experience store。整个
`/srv/aer/knowledge` 删掉，代价只是重建一次（一千条约两秒），不是丢数据。

```python
with AER("./data", knowledge_dir="./knowledge") as aer:
    aer.project_experiences()                     # SQLite → NeuG

    result = aer.retrieve("WordPress REST API 403", domain="wordpress")
    print([hit.label for hit in result.guidance])   # ['Verified Recovery']
    print([hit.label for hit in result.warnings])   # ['Known Failure']

    print(aer.experience_context("WordPress REST API 403"))   # 直接可注入的文本
```

### 三个角色，永远不混

| 标签 | 含义 | 出现在 |
| --- | --- | --- |
| `[Verified Success]` | 独立验证通过、一步到位 | `guidance` |
| `[Verified Recovery]` | 失败后修好、且结果被验证 | `guidance` |
| `[Known Failure]` | 确认不管用的做法 | `warnings` |
| `[Unverified Recovery Observation]` | 只有 Agent 自述，没有外部确认 | `warnings`（仅 DIAGNOSTIC） |

`kind=FAILURE` 的记录**只进 `warnings`**，而且格式上不可能输出 `Solution:` ——
唯一比不检索更糟的结果，是让 Agent 把"试过、没用"当成方案去执行。

### 运维

```bash
python -m aer.knowledge status     # 可达性 / 投影版本 / 两个计数 / drift
python -m aer.knowledge project    # 增量补齐（把还没投影的经验补上）
python -m aer.knowledge rebuild    # 从 SQLite 重建并原子替换（万能的修法）
```

知识库丢了、坏了、版本不符——三种情况的修法都是 `rebuild`，
因为它的每一个字节都能从 SQLite 推导出来。

### 这一版不做什么

不做 embedding、不做 HNSW 向量检索、不记录 `reuse_count`、不做 Workflow / Dataset。
原因见 `docs/DECISIONS.md` D-056 与 D-060：**目前没有真实检索数据证明 BM25 不够用**，
也没有数据能定义"这次检索有用"。

## 部署

完整步骤见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。要点：

```text
Commit → PR → CI → merge main → 构建不可变镜像 → 推送 GHCR
       → SSH 生产 → 备份 SQLite → Alembic 迁移 → 生产冒烟 → 记录 deployed SHA
```

```bash
# 服务器上（脚本随镜像一起发布；服务器不需要 Python 和构建工具）
cd /srv/aer/deploy
bash ./deploy.sh --image ghcr.io/<owner>/aer:sha-<commit> --git-sha <commit>
bash ./deploy.sh --image ... --git-sha ... --dry-run    # 只看会做什么
bash ./rollback.sh                                      # 回到上一版本镜像

# 排障：对生产库做只读冒烟
docker compose -f compose.yaml run --rm aer-runtime python /app/scripts/smoke_test.py
```

**灾备不是承诺，是已执行过的事实**：备份已在隔离目录被真实恢复、迁移，并由线上镜像冒烟
通过（6/6）；恢复也可以直接从**只读挂载**的备份进行，那是灾难现场的常态。

**跨版本恢复也已验证**：一份 revision `0003` 的备份，经当前镜像恢复后前向迁移到 head
（`0004`），历史记录逐字段保全、新表为空、当前 Runtime 既能读旧数据也能继续写新数据。
记录见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) 第 15、16 节。

三条必须知道的边界：

- **数据不在镜像里。** SQLite 只存在于 bind mount 的持久化目录（`/srv/aer/data`），
  镜像随时可删可重建。
- **服务器不构建源码。** 它只 `docker pull` 一个由 CI 构建、带 commit 标签的镜像。
- **回滚不降级数据库。** 镜像回滚与数据库恢复是两个独立决定；不兼容时显式从迁移前
  备份恢复（见 `docs/DECISIONS.md` D-044）。

---

## 设计约束

- **SQLite First / Embedded First / Single Process First**
- 单机可运行，不依赖 Redis、Kafka、PostgreSQL、独立向量库
- 同步优先，确定性代码优先
- 耗时用单调时钟（`perf_counter`），时刻用 timezone-aware wall clock
- 未知 payload 降级为显式 marker，不静默 `str()`
- Hook 只负责观察 / 记录 / 关联 / 测量，不含业务知识
- 验证器 / 提炼器崩溃 ≠ 结论：崩溃记录 `ERROR`，绝不伪造 `passed=false`
  或半条 `Experience`
- 终态 Run 后禁止 Agent 变更，但允许 System Observation（白名单）
- 经验状态只能经 `transition_to()` / `mark_verified()` 变更（模型 frozen）
- `kind` 与 `status` 由确定性代码决定，模型只能提供被记录的**建议**
- 所有长期数据写入前必须经过 Sanitizer（后续 Milestone 扩展）
- 禁止 Agent 自评替代 Verifier

完整开发边界与优先级见 [`agent.md`](agent.md) 与 [`docs/TASKS.md`](docs/TASKS.md)，
架构决策记录见 [`docs/DECISIONS.md`](docs/DECISIONS.md)（D-029 起为本轮新增）。

---

## 许可证

Apache License 2.0 —— 全文见 [`LICENSE`](LICENSE)。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。里面最硬的一条：**已发布的 Alembic revision
不允许改写**——schema 变更必须新增 revision，并且必须让历史数据库仍然能升到 head。

## 安全

见 [`SECURITY.md`](SECURITY.md)。

**请不要用公开 issue 报告漏洞。** 首选 GitHub 的 Private Vulnerability Reporting；
该功能在本仓库**尚未开启**，需要管理员在 Settings → Security 中打开。在它开启之前，
请用 `SECURITY.md` 里的降级流程：issue 里只问私密渠道，不带任何细节。

也请读一下那里写明的**设计限制**——尤其是这一条：**Sanitizer 尚未完整实现**，
调用方传入的结构化 payload（tool input/output、event payload、verification message）
是**原样落库**的。不要依赖 AER 替你过滤凭据或个人信息。
