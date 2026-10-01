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

## 完整闭环验收

最新 M7/M8 开发代码的安装与验收见 [可用版本说明](docs/USABLE_VERSION.md)。
源码安装检索依赖后，还需执行 `python scripts/install_neug_extensions.py` 安装 FTS 扩展。
运行 `python -m aer.demo --output-dir ./demo-results` 可验证本地失败、恢复、提炼、
真实检索、上下文消费、使用追踪与重启持久化；示例不需要模型凭据或真实 WP 站点。

## 安装

要求 **Python >= 3.12**。

```bash
pip install aer-runtime                 # 运行时 / 独立验证 / 经验提炼 / SQLite 存储
pip install "aer-runtime[knowledge]"    # 再加上检索（NeuG 知识索引）
```

`neug` 是**可选依赖**，而不是把必需的东西藏进 extra：它在 PyPI 上只有 macOS(arm64) 与 Linux
的 wheel，**没有 Windows 分发**，声明为必需会让 Windows 上整条 `pip install` 直接失败
（`docs/DECISIONS.md` D-065）。没有它时 `import aer`、`AER()`、验证与提炼全部正常，
只有引擎侧检索不可用——引擎在 `aer/knowledge/neug.py` 里是**惰性导入**的，
测试也钉住了这一点。

从源码（要改代码时）：

```bash
git clone https://github.com/Wike-CHI/aer.git
cd aer
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,knowledge]"                # Windows 上写 -e ".[dev]"
```

发行版由 GitHub Release 触发 [`.github/workflows/publish.yml`](.github/workflows/publish.yml)
发布到 PyPI（Trusted Publishing，仓库里没有任何凭据）。

---

## 当前状态

```text
Milestone 1 — Runtime Core           ✅
Milestone 2 — SQLite Storage         ✅
Milestone 3 — Hook & Trace           ✅
Milestone 4 — Verification           ✅
Milestone 5 — Experience Core        ✅
Milestone 6 — NeuG Retrieval         ✅
Milestone 7 — Experience Usage       ✅
Milestone 8 — Agent Adapter Protocol ✅
Infrastructure — Repo / Docker / CI / CD / Backup  ✅
```

已实现：

- 统一枚举：`RunStatus` / `EventType` / `VerifierType`（按信任度声明）/
  `ExperienceKind` / `ExperienceStatus` / `DistillationTrigger` /
  `RetrievalMode` / `UsageSignal` / `UsageSignalSource` / `UtilityLabel` /
  `UtilitySource` / `UsageRole` / `RunOutcome` / `SessionAssignment` /
  `AdapterIngestOutcome` / `AdapterSessionOutcome`
- 领域模型：`Run` / `Event` / `ErrorRecord` / `RecoveryRecord` /
  `VerificationRecord` / `Experience` / `ExperienceSource` /
  `RetrievalSession` / `ExperienceUsage` / `AdapterSession` / `AdapterEventRecord`
- SQLite 存储：WAL + 外键 + `synchronous=NORMAL` + `busy_timeout=5000`
- 十一张表：`runs` / `events` / `errors` / `recoveries` / `verifications` /
  `experiences` / `experience_sources` / `retrieval_sessions` /
  `experience_usage` / `adapter_sessions` / `adapter_events`（+ `alembic_version`）
- **Schema 由 Alembic 管理**（`alembic upgrade head`，revision 0001–0006），
  不再依赖 `create_all`
- Repository 层：`Run` / `Event` / `Error` / `Recovery` / `Verification` /
  `Experience` / `ExperienceSource` / `RetrievalSession` / `ExperienceUsage` /
  `AdapterSession` / `AdapterEvent`
- 嵌入式运行时：`AER` + `RunContext`
- **Hook 上下文管理器**：`run.tool()` / `run.recovery()`，异常路径也保证写出结束事件
- 统一 Error 管道：`run.error()`，结构化落库 + 脱敏后的 stack trace
- **独立验证引擎**：`run.verify()` → `VERIFICATION` 事件 + `VerificationRecord`
- 四类验证器：`DETERMINISTIC`（`HttpStatusVerifier` / `H1CountVerifier` /
  `JsonValidVerifier` / `PredicateVerifier`）、`ENVIRONMENT`、`HUMAN`、`LLM`
- **`verified_success`**：区分「Agent 说完成」与「独立确认完成」
- **经验提炼**：`run.distill()` → `Experience` + `ExperienceSource`，
  三类经验 `SUCCESS` / `RECOVERY` / `FAILURE`，生命周期受状态机约束
- **知识检索**：SQLite → NeuG 单向投影，BM25 + 图过滤 + 策略/排序/角色化文本；
  NeuG 是可重建副本，SQLite 始终是唯一事实源
- **经验使用与效果**：`retrieve_for_run()` / `record_injection()` /
  `record_usage_signal()` / `record_utility()` →
  `experience_effectiveness()` / `experience_confidence()` /
  `promote_experience()`（`VERIFIED → REUSED → PROVEN`）
- **Agent Adapter Protocol**：外部 Agent 通过同一协议接入；Adapter 只做翻译，
  AER Core 不认识任何厂商格式。含 `GenericAgentAdapter` 参考实现、会话映射与
  幂等账本、外部输入脱敏与尺寸上限
- Event `sequence` 严格递增，自动写入 `TASK_START` / `TASK_END`
- **可部署制品**：`Dockerfile`（非 root、可丢弃、OCI 标签带 commit）、
  `deploy/compose.yaml`、`scripts/{deploy,rollback,smoke_test}.sh`、
  `scripts/{backup,restore}_sqlite.py`、`.github/workflows/{ci,deploy}.yml`
- **灾备可验证**：备份/恢复工具、`scripts/drill_{facts,compare,seed}.py`
  与已真实执行过的[灾难恢复演练](docs/DEPLOYMENT.md#15-灾难恢复演练disaster-recovery-drill)

尚未实现（后续 Milestone）：Codex / Claude Code / Cursor / DSH 的真实
Adapter（M8.1–M8.4）、Dataset Builder、Workflow、完整 Sanitizer、Artifact、
CLI、FastAPI、Dashboard、Embedding / HNSW、模型训练。

本轮**刻意不做**（见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) 第 16 节）：
Kubernetes / Helm / Terraform / Ansible、Redis / Kafka / PostgreSQL、
日志聚合与指标系统、HTTP healthcheck。

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
 │          │            │           │          │
 └──────────┴────────────┴───────────┴──────────┴─────────► DEPRECATED（终态）

M7 起前五个状态可达：
  VERIFIED → REUSED   被注入到一个非来源 Run（由 promotion policy 判定）
  REUSED   → PROVEN   需要 adoption 信号 + 多个不同 Run 的 verified success
  TRAINING_CANDIDATE / TRAINING_DATA 仍然没有任何入边：把经验变成训练数据需要
  Dataset Builder、去重、安全审查与 Holdout 隔离（D-074）
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
├── knowledge/                # M6：可检索投影
│   ├── base.py               # KnowledgeIndex 协议（上层永远看不到 NeuG）
│   ├── schema.py             # 投影 schema V1 + 版本号
│   ├── query.py              # 用户文本 → 安全全文表达式
│   ├── neug.py               # 唯一 import neug 的模块（惰性）
│   ├── projector.py          # 单向复制 / drift / 分阶段 rebuild
│   ├── retriever.py          # 策略 → 索引 → 排序 → 分流
│   ├── ranking.py            # 打分公式（文本为乘法闸门）
│   ├── formatter.py          # 角色化文本（标签就是契约）
│   ├── models.py             # ExperienceSearchQuery / RetrievalHit / RetrievalResult
│   ├── status.py / cli.py    # 运维面
│   └── neug_fts 扩展由镜像构建期安装
├── usage/                    # M7：经验使用与效果
│   ├── fingerprints.py       # query 脱敏 + query/context 指纹
│   ├── tracking.py           # 唯一写 usage 的模块（session / 注入 / 信号 / 效用）
│   ├── effectiveness.py      # usage + runs + verifications → 报告
│   ├── promotion.py          # VERIFIED → REUSED → PROVEN（阈值可配置）
│   └── confidence.py         # 确定性、可拆解的 confidence（按需计算）
├── adapter/                  # M8：Agent Adapter Protocol
│   ├── protocol.py           # 协议版本 / identity / capabilities / envelope / AgentAdapter
│   ├── sanitize.py           # 外部输入治理（脱敏 / 丢弃私有推理 / 尺寸上限）
│   ├── registry.py           # adapter_name → factory（含协议版本校验）
│   ├── ingest.py             # AdapterIngestor：会话 / 幂等 / 分发 / 能力门禁
│   └── generic.py            # GenericAgentAdapter（参考实现）
├── storage/
│   ├── connection.py / database.py / migrations.py / converters.py
│   ├── models.py             # 十一张业务表
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
├── 0004_experiences_and_sources.py
├── 0005_retrieval_sessions_and_usage.py
└── 0006_adapter_sessions_and_events.py

tests/
├── runtime/                  # 领域模型 / 生命周期 / 事件 / 三个 Hook
├── verification/             # 原语 / 协议 / 引擎 / 崩溃语义 / 终态守卫 / 汇总
├── experience/               # 模型 / 状态机 / 策略 / 证据 / 提炼 / 去重 / 来源幂等
├── knowledge/                # 投影 / 检索 / 排序 / 格式化 / 引擎（无 wheel 时跳过）
├── usage/                    # M7：session / usage 行 / 注入 / 信号 / 效果 / 晋升 / 置信度
├── adapters/                 # M8：协议 / 幂等 / 会话 / 脱敏 / 事件映射 / 两个假 Agent
├── storage/                  # 初始化 / 迁移 / 仓储 / 持久化
├── infra/                    # 部署配置 / 备份恢复 / compose / Dockerfile / workflow
└── integration/              # M3 失败-恢复 / M4 Agent-Verifier 冲突 / M5 三类经验 / M7 效果

data/
└── aer.db                    # 默认数据库（不纳入版本控制）
```

分层原则：

```text
Business Agent
     ↓
AER SDK (AER / RunContext / ToolContext / RecoveryContext)
     ↓
Experience            Verification        Knowledge            Usage
(Evidence/Policy/     (Verifier/          (Projector/          (Tracking/
 Provider/Service)     Engine)             Retriever/           Effectiveness/
                                           Ranking/Formatter)   Promotion/Confidence)
     ↓                     ↓                     ↓                    ↓
Repository (Run / Event / Error / Recovery / Verification / Experience /
            ExperienceSource / RetrievalSession / ExperienceUsage /
            AdapterSession / AdapterEvent)
     ↑
Adapter (外部 Agent → 协议 envelope → AdapterIngestor → RunContext.external)
     ↓
SQLAlchemy ORM
     ↓
SQLite  ◄── 唯一事实源           NeuG ◄── 可丢弃的检索投影（Usage 不写它）
```

- `aer.runtime` 是领域层，**不 import** `experience` / `verification` / `knowledge` /
  `usage` / `adapter` / 任何存储细节；
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

运行时依赖：Python 3.12+、pydantic 2.x、SQLAlchemy 2.x、Alembic 1.13+。
开发额外需要 pytest / ruff / mypy / pyyaml（最后一个用于实际解析 compose 与
workflow 文件，而不是对它们做文本匹配）。

```bash
pip install -e ".[dev,knowledge]"   # Windows 上写 -e ".[dev]"（neug 无 Windows 分发）

pytest
ruff check .
ruff format --check .
mypy aer/
```

`knowledge` extra 只在能装 `neug` 的平台上加。少了它，引擎相关的用例会被跳过而不是失败，
所以**本机全绿不等于 CI 全绿**——CI 用的是 `.[dev,knowledge]`。

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

不做 embedding、不做 HNSW 向量检索、不做 Workflow / Dataset。
原因见 `docs/DECISIONS.md` D-056：**目前没有真实检索数据证明 BM25 不够用**。
"这次检索到底有没有用"是 M7 的问题，见下一节。

---

## 经验使用：检索 ≠ 注入 ≠ 采用 ≠ 有用

M7 回答的是检索之后的问题。四个区别必须先说清楚，因为把它们合并起来
恰恰是这个系统最容易犯、也最有害的错误：

```text
Retrieved  ≠  Injected    检索结果没人渲染   ≠  被使用
Injected   ≠  Adopted     进了上下文         ≠  Agent 采用了
Adopted    ≠  Helpful     Agent 可能采用了一条错误的经验
Success    ≠  Caused      任务成功           ≠  这条经验导致了成功
```

因此：**被检索不会让任何计数器 +1**；**注入 + Run SUCCESS 也不会自动判定经验有效**。

```python
with AER("./data", knowledge_dir="./knowledge") as aer:
    # 1) 检索并记账（retrieve() 仍然纯读，见下图）
    tracked = aer.retrieve_for_run(
        query="WordPress REST API 403", run_id=run.run_id, domain="wordpress",
    )

    # 2) 真正进入 Agent 上下文的是哪几条（可以只注入结果的一部分）
    hits = list(tracked.result.guidance)
    formatter = ExperienceContextFormatter()
    aer.record_injection(
        session_id=tracked.session_id,
        experience_ids=[hit.experience_id for hit in hits],
        context_fingerprint=context_fingerprint(formatter.format(tracked.result)),
        formatter_version=FORMATTER_VERSION,
        positions={hit.experience_id: rank for rank, hit in enumerate(hits, 1)},
        char_counts={hit.experience_id: len(formatter.format_hit(hit)) for hit in hits},
    )

    # 3) Agent / 人类明确说：用了 / 明确没用 / 明确不适用
    aer.record_usage_signal(
        session_id=tracked.session_id, experience_id=hits[0].experience_id,
        signal=UsageSignal.ADOPTED, source=UsageSignalSource.AGENT,
    )
    aer.record_utility(          # 有用没用是另一个判断，来源也要留
        session_id=tracked.session_id, experience_id=hits[0].experience_id,
        label=UtilityLabel.HELPFUL, source=UtilitySource.HUMAN,
    )

    # 4) 看观察到的证据（不是因果结论）
    print(aer.experience_effectiveness(hits[0].experience_id).describe())
    print(aer.experience_confidence(hits[0].experience_id).describe())

    # 5) 达到阈值才升级状态
    aer.promote_experience(hits[0].experience_id)   # VERIFIED → REUSED → PROVEN
```

```text
retrieve()           纯读，不写任何 usage（后台查询、调试、测试都用它）
retrieve_for_run()   显式记账：写 retrieval_sessions + experience_usage
```

Usage 存 SQLite，**不进 NeuG**；NeuG 在这一轮完全不参与记账（D-066）。

### 数据模型

```text
retrieval_sessions   一次检索一行，0 结果也写（"查过但没有"≠"根本没查"）
experience_usage     一次检索 × 一条经验，UNIQUE(session, experience)
```

```text
query_text          sanitized（脱敏 + 折叠空白 + 截断），绝不存原始 prompt
query_fingerprint   从 sanitized 文本派生，用于统计同类检索
usage_signal        UNKNOWN / ADOPTED / IGNORED / REJECTED  + source
utility_label       UNKNOWN / HELPFUL / NEUTRAL / HARMFUL   + source
injected_*          injected_at / position / chars / context_fingerprint / formatter_version
```

不存完整 prompt、不存渲染后的 context、不存 chain-of-thought——只存指纹与结构化信号。

### Effectiveness 报告

```text
retrieval_count / injection_count / explicit_{adoption,ignore,rejection}_count
helpful_count / neutral_count / harmful_count
distinct_target_runs
verified_success_runs / verified_failure_runs / run_failed_runs / unverified_runs
observed_success_rate / injected_verified_success_rate / adopted_verified_success_rate
```

三条规则：

```text
计数单位是「不同的 run」，不是 usage 行（一次 Run 里检索十次只是一次证据）
分母只含「已 Injected 且 target run 有 Verification」的 run；UNVERIFIED 不算失败
adoption 子集单独报告，不与 injected 口径混合
```

`observed_success_rate` 的名字就是边界：

```text
P(success | experience injected)  ≠  P(success | do(experience injected))
```

这是**观察到的相关性**，不是因果效果。真正的因果证据需要 randomized holdout，
字段（`experiment_id` / `assignment`）已预留，本轮不实现。

### Promotion：REUSED 与 PROVEN

```text
VERIFIED → REUSED   被注入到一个不是它来源 Run 的真实 Run（不要求该 Run 成功）
REUSED   → PROVEN   阈值全部可配置（PromotionPolicy）：
                      distinct adopted runs        >= 5
                      adopted verified successes   >= 4
                      adopted success rate         >= 0.80
                      harmful feedback             == 0
```

```text
仅被检索              不算 reuse
被自己的 source run 注入  不算 reuse
只有 Injected 全 UNKNOWN  可以 REUSED，默认不得自动 PROVEN
```

`PROVEN` 的语义是"在多个真实任务中被明确采用，并与多次 verified success 共现"，
仍然不是 causally proven。

### 这一版不做什么

不做 Agent Adapter（接口留给 M8）、不做 Dataset Builder、不做训练、不做 A/B 实验、
不做 Dashboard / FastAPI、不做 HNSW / Embedding、不把 usage 投影进 NeuG、
不自动从行为推断 adoption。原因见 `docs/DECISIONS.md` D-069 与 D-074。

## 接入其它 Agent：一个协议，多个 Agent

M8 解决的是**规模**问题，不是编码问题：AER 的价值来自真实执行量，而那个量在别人的
Agent 里（Codex / Claude Code / Cursor / DSH / 内部 Agent）。没有协议时，每个接入都是
一段伸手进 AER 内部的定制脚本，而第二个接入会悄悄和第一个对"事件"的理解不一致。

```text
Codex / Claude Code / Cursor / DSH / Internal Agent
        │
        ▼  厂商专属翻译（M8.1–M8.4，在 AER 之外）
Agent-specific Adapter
        │
        ▼  Agent Adapter Protocol   ← 只有这一层是 AER 的一部分
        ▼
AER Runtime
```

**一句话**：Adapter 只做 `Translate / Normalize / Sanitize / Associate`；
AER Core 永远不认识 Claude hook、Codex event、Cursor transcript 或 DSH session。

```python
from aer import AER, GenericAgentAdapter

with AER("./data") as aer:
    aer.adapter_registry.register(GenericAgentAdapter)

    # 1) 外部会话 → AER Run（重连会复用同一个 Run）
    handle = aer.open_adapter_session(
        "aer-generic", {"session_id": "conv-7", "task": "fix the REST API 403"},
    )

    # 2) 厂商事件 → 协议 envelope → Run（同一 external_event_id 重投是 no-op）
    aer.ingest_adapter_event(
        handle, {"type": "tool_call", "tool": "shell", "event_id": "e1"},
    )
    aer.ingest_adapter_event(
        handle,
        {"type": "tool_result", "tool": "shell", "success": False,
         "error": {"error_type": "shell.NonZeroExit", "message": "403"},
         "event_id": "e2"},
    )

    # 3) 检索 / 注入 / 采用：Adapter 是 record_injection 的权威调用方
    tracked = aer.adapter_ingestor.retrieve(handle, "WordPress REST API 403")
    aer.adapter_ingestor.inject(handle, tracked)

    # 4) 结束
    aer.close_adapter_session(handle, {"status": "success"})
```

### 四条边界

```text
1. Core 不认识厂商格式      Adapter 适配 AER，不是反过来。不新增 EventType。
2. Adapter 不碰 Repository  只能走 Runtime API（run.external），否则会绕过
                           terminal guard、错误管道与 sequence 分配。
3. 外部输入一律不可信       按值 + 按键脱敏、丢弃私有推理、尺寸上限 + 显式截断。
                           Prompt injection 结构上不可达：协议里没有能让外部
                           字符串变成指令的字段。
4. 能力声明是强制的         不能观测"Agent 有没有采用"就只能留 UNKNOWN。
                           Adapter 永远不许因为任务成功就写 HELPFUL。
```

### 幂等与会话

```text
adapter_sessions   (provider, external_session_id) 唯一 → 当前 Run
                   重连复用 RUNNING 的 Run；已终态默认拒绝，显式 reopen() 才新建
adapter_events     (provider, external_event_id) 唯一 → 幂等账本
                   先 claim → 再 apply → 再 mark_applied
                   重复投递 = DUPLICATE，不写任何东西
```

### 运维面

```python
print(aer.adapter_status())
# {'protocol_version': '1', 'sessions': 1, 'events': 2, 'unapplied_events': 0,
#  'adapters': [...]}

aer.adapter_sessions.list()                      # external session → run 映射
aer.adapter_events.list(aer_run_id=run_id)       # 这条 Run 收到过哪些外部事件
```

没有 Dashboard、没有 FastAPI、没有 Plugin Marketplace——注册表就是一个 dict。

### 这一版不做什么

不做任何**真实**厂商 Adapter（Codex / Claude Code / Cursor / DSH 属于 M8.1–M8.4）、
不做 Dataset Export、不重写 M1–M4 的 Event 语义。参考实现
`GenericAgentAdapter` 不是给生产用的：它的作用是证明协议足够——两个完全不同风格的
假 Agent 通过它产生逐条相同的 AER 语义。

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
（当时是 `0004`），历史记录逐字段保全、新表为空、当前 Runtime 既能读旧数据也能继续写
新数据。记录见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) 第 15、16 节。
M7 的 `0004 → 0005` 也按同样的方式验证（见第 19 节）：旧 Experience 完整保留，
`retrieval_sessions` 与 `experience_usage` 为空表。
M8 的 `0005 → 0006` 同样验证（见第 23 节）：usage 记录原样保留，
`adapter_sessions` 与 `adapter_events` 为空表。

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

## Codex 接入：用公开生命周期 Hook，不补丁、不代理

M8.1 把 Codex CLI 的生命周期事件接进 AER。用的是**公开 hook 接口**
（`~/.codex/hooks.json`），不 fork、不打补丁、不新建服务（第 43、61 节）。

```bash
# 1) 让 Codex 在 12 个生命周期事件上调用 AER 的 hook 命令
python -m aer.adapter.codex.hook --print-coverage    # 先看它被验证能观测到什么

# 2) 把 hooks.json 指向它（绝对路径，且**不要给程序路径加引号**——
#    实测加了引号会让 Codex 静默启动失败，看起来就像 hook 没触发）
```

```text
Codex hook (stdin JSON) → aer.adapter.codex → Agent Execution Envelope → AdapterIngestor
```

这条链路上的四条边界：

```text
1. Codex 自述成功 ≠ Verification        Adapter 没有 external_verification 能力
2. 工具成功 ≠ Experience 被采用          Adapter 没有 explicit_adoption_signal 能力
3. hook 执行成功 ≠ Experience 已注入     AER 不会因此写 record_injection
4. 会话结束 ≠ 任务成功，也 ≠ 任务失败     SessionEnd 未声明结果时 Run 关为 INCONCLUSIVE
                                        （AER 的一种状态：结束了，但没人说结果如何）
```

**覆盖矩阵是交付物的一部分**，不是脚注：

```bash
python scripts/probe_codex_hooks.py            # 免费：不花 token，不需要登录
python scripts/probe_codex_hooks.py --use-auth # 需要一次真实回合（会消耗额度）
```

未观测到的事件一律标为 `MISSING`，因为"没有工具事件的 Run"和"这次没用工具"
在数据里长得一样——把缺口写出来，才能区分这两种情况。

完整说明、覆盖矩阵与安装步骤见 [`docs/CODEX_ADAPTER.md`](docs/CODEX_ADAPTER.md)。
