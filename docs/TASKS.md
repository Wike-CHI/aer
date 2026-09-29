# AER MVP Development Tasks

## 0. Document Purpose

本文档用于指导 AER（Agent Experience Runtime，智能体经验运行时）MVP 的实际开发。

本阶段目标不是构建完整平台，而是验证最核心假设：

> Agent 是否能够通过记录、验证、提炼和复用历史经验，减少重复踩坑并提高后续任务成功率。

开发必须严格遵循：

```text
P0 → P1 → P2
```

在 P0 未完成并通过真实任务验证前，不得提前实现 P2 能力。

---

# 1. MVP Definition

MVP 必须跑通以下完整闭环：

```text
Run A
↓
Agent 执行任务
↓
Hook 捕获执行轨迹
↓
出现错误
↓
Agent 恢复
↓
Verifier 验证成功
↓
Experience Distiller 提炼经验
↓
Experience Store 保存
↓
Run B 遇到类似问题
↓
Retrieval 检索 Experience
↓
Experience 注入 Agent
↓
Agent 使用历史经验
↓
Verifier 验证成功
↓
记录 Experience 是否有效
```

如果这个闭环没有真实跑通，则 MVP 不算完成。

---

# 2. Development Principles

所有开发任务必须遵循以下原则：

```text
SQLite First
Embedded First
Single Process First
Deterministic First
Verification First
Experience First
Training Later
```

禁止为了未来扩展性提前引入：

```text
Kafka
Redis
PostgreSQL
Microservices
Vector Database
Kubernetes
Complex RBAC
Model Training
```

---

# 3. Milestone Overview

开发分为 8 个里程碑：

```text
M1 Runtime Core
M2 SQLite Storage
M3 Hook & Trace
M4 Verification
M5 Experience
M6 Retrieval
M7 Experience Usage
M8 Real-world Validation
```

只有完成前一个 Milestone，才进入下一个。

---

# 4. Milestone 1 — Runtime Core

## Goal

建立 AER 最基础的运行时对象：

```text
Run
Event
Error
Recovery
Verification
```

---

## Task 1.1 — Define Domain Models

创建：

```text
aer/runtime/models.py
```

定义：

```python
Run
Event
ErrorRecord
RecoveryRecord
VerificationRecord
```

推荐使用：

```text
Pydantic Model
```

或：

```text
Dataclass
```

要求：

所有核心对象必须拥有明确类型定义。

禁止核心业务层大量使用裸 `dict`。

### Acceptance Criteria

必须支持：

```python
run = Run(...)
event = Event(...)
```

并完成 Pydantic 校验。

---

## Task 1.2 — Define Run Status

创建统一枚举：

```python
RUNNING
SUCCESS
PARTIAL_SUCCESS
FAILED
ABORTED
```

禁止在其他模块自行创造 Run Status。

### Acceptance Criteria

非法状态必须被拒绝。

---

## Task 1.3 — Define Event Types

统一事件类型：

```python
TASK_START
MODEL_CALL
MODEL_RESULT
TOOL_CALL
TOOL_RESULT
ERROR
RECOVERY_START
RECOVERY_RESULT
VERIFICATION
HUMAN_FEEDBACK
TASK_END
```

### Acceptance Criteria

Event 必须包含：

```text
run_id
sequence
event_type
created_at
```

---

# 5. Milestone 2 — SQLite Storage

## Goal

建立可持久化的 SQLite 数据层。

---

## Task 2.1 — Database Initialization

创建：

```text
aer/storage/database.py
```

启动 SQLite 时执行：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

默认数据库：

```text
./data/aer.db
```

### Acceptance Criteria

首次运行：

```text
data/aer.db
```

自动创建。

---

## Task 2.2 — Database Schema

建立以下表：

```text
runs
events
artifacts
errors
verifications
experiences
experience_sources
experience_usage
workflows
dataset_items
```

MVP 允许后 4 张表稍后启用，但 Schema 应提前规划。

---

## Task 2.3 — Alembic

配置：

```text
Alembic
```

Alembic：

> Python 数据库 Schema 版本迁移工具。

要求：

数据库结构修改必须通过 Migration。

禁止手动修改生产数据库 Schema。

### Acceptance Criteria

以下命令可运行：

```bash
alembic upgrade head
```

---

## Task 2.4 — Repository Layer

创建：

```text
aer/storage/repositories/
```

至少实现：

```python
RunRepository
EventRepository
VerificationRepository
ExperienceRepository
```

业务层禁止直接执行 SQL。

---

## Task 2.5 — SQLite Persistence Test

测试流程：

```text
创建 Run
↓
创建 Event
↓
关闭数据库
↓
重新初始化
↓
读取 Run
↓
读取 Events
```

必须保持数据一致。

---

# 6. Milestone 3 — Hook & Trace

## Goal

让 Agent 执行过程能够自动生成完整 Trace。

Trace：

> 一次任务的完整调用链和执行轨迹。

---

## Task 3.1 — AER Runtime

实现：

```python
from aer import AER

aer = AER("./data")
```

---

## Task 3.2 — start_run()

实现：

```python
run = aer.start_run(
    task="..."
)
```

行为：

```text
创建 Run
生成 Run ID
写入 TASK_START
返回 RunContext
```

---

## Task 3.3 — Event Sequence

每个 Run 内 Event 必须拥有严格递增：

```text
sequence
```

例如：

```text
1
2
3
4
5
```

禁止依赖时间戳排序作为唯一顺序依据。

---

## Task 3.4 — Tool Context Manager

实现：

```python
with run.tool("wordpress.update_page") as tool:
    result = update_page()
    tool.set_result(result)
```

自动产生：

```text
TOOL_CALL
TOOL_RESULT
```

自动记录：

```text
开始时间
结束时间
duration_ms
异常
```

---

## Task 3.5 — Exception Capture

如果：

```python
with run.tool(...):
    raise Exception(...)
```

AER 必须自动：

```text
记录 ERROR
记录 Tool Failure
保留 Exception
```

不能导致 Trace 丢失。

---

## Task 3.6 — Recovery Hook

实现：

```python
with run.recovery(
    reason="REST API 403"
):
    ...
```

产生：

```text
RECOVERY_START
RECOVERY_RESULT
```

---

## Task 3.7 — Run Completion

实现：

```python
run.success()
run.fail()
run.abort()
```

自动生成：

```text
TASK_END
```

同时更新：

```text
ended_at
status
```

---

# 7. Milestone 4 — Verification

## Goal

禁止 Agent 自己说“完成”就认为任务成功。

---

## Task 4.1 — Verifier Interface

创建：

```text
aer/verification/base.py
```

接口：

```python
class Verifier:
    def verify(self, context) -> VerificationResult:
        ...
```

---

## Task 4.2 — Verification Types

实现枚举：

```text
DETERMINISTIC
ENVIRONMENT
LLM
HUMAN
```

---

## Task 4.3 — Deterministic Verifier

实现最基础的代码验证能力。

示例：

```python
HttpStatusVerifier
H1CountVerifier
JsonSchemaVerifier
DomExistsVerifier
```

---

## Task 4.4 — WordPress Verifier

第一个业务 Verifier：

```text
aer/verification/wordpress.py
```

至少支持：

```text
HTTP Status
H1 Count
Title Exists
Meta Exists
Schema Valid
DOM Selector Exists
```

---

## Task 4.5 — Verification Rule

Run 不能仅因为：

```python
run.success()
```

就成为最终可信成功。

必须区分：

```text
Agent Status
```

和：

```text
Verified Status
```

### Acceptance Criteria

系统必须可以表示：

```text
Agent says SUCCESS
Verifier says FAILED
```

---

# 8. Milestone 5 — Experience

## Goal

把高价值 Run 提炼为 Experience。

---

## Task 5.1 — Experience Model

字段：

```text
id
domain
title
problem
symptoms
root_cause
solution
workflow
avoid
status
confidence
reuse_count
success_count
failure_count
created_at
updated_at
```

---

## Task 5.2 — Experience Status Machine

状态词汇：

```text
RAW
DISTILLED
VERIFIED
REUSED
PROVEN
TRAINING_CANDIDATE
TRAINING_DATA
DEPRECATED
```

当前允许的转换：

```text
RAW        → DISTILLED | DEPRECATED
DISTILLED  → VERIFIED | DEPRECATED
VERIFIED   → DEPRECATED
DEPRECATED → （终态，无出边）
```

`REUSED` / `PROVEN` / `TRAINING_CANDIDATE` / `TRAINING_DATA` 目前**没有任何入边**：
它们需要 `experience_usage` 统计（M7）。禁止自动晋升。

实现明确状态转换规则。

禁止：

```text
RAW
→
TRAINING_DATA

RAW
→
PROVEN

VERIFIED
→
TRAINING_CANDIDATE
```

状态顺序说明：原文档写作 `RAW → VERIFIED → DISTILLED`，M5 修正为
`RAW → DISTILLED → VERIFIED`。理由是验证针对的是**提炼出的陈述**，陈述不存在时
无从验证。详见 `docs/DECISIONS.md` D-029 与 `agent.md #24`。

---

## Task 5.3 — Distillation Trigger

只有以下 Run 自动进入 Distiller：

```text
存在 Error
存在 Recovery
存在 Human Feedback
首次出现的问题
高价值任务
多次 Retry
Verifier 与 Agent 判断冲突
```

普通直接成功任务默认不进入。

---

## Task 5.4 — Experience Distiller

接口：

```python
distill(run_id) -> ExperienceCandidate
```

输入：

```text
Run
Events
Errors
Recoveries
Verifications
```

输出：

```json
{
  "title": "",
  "problem": "",
  "symptoms": [],
  "failed_attempts": [],
  "root_cause": "",
  "solution": "",
  "recommended_workflow": [],
  "avoid": [],
  "generalizable": true
}
```

---

## Task 5.5 — Experience Source

建立：

```text
experience_sources
```

允许：

```text
Experience A
← Run 1
← Run 13
← Run 25
```

重复问题不要创建大量重复 Experience。

---

## Task 5.6 — Experience Deduplication

第一版使用：

```text
Title
Problem
Root Cause
Keyword
```

做简单去重。

以后再增加 Embedding。

---

# 9. Milestone 6 — Retrieval

## Goal

新任务可以查找历史 Experience。

---

## Task 6.1 — SQLite FTS5

创建 Experience 全文搜索表。

FTS5：

> SQLite Full-Text Search 5，SQLite 原生全文搜索模块。

搜索字段：

```text
title
problem
root_cause
solution
```

---

## Task 6.2 — Keyword Retriever

接口：

```python
search_experience(
    query: str,
    domain: str | None,
    limit: int = 3
)
```

默认：

```text
Top 3
```

最大：

```text
Top 5
```

---

## Task 6.3 — Experience Ranker

第一版综合：

```text
FTS Match
Confidence
Success Rate
Freshness
Domain Match
```

不要仅按照全文搜索相关度排序。

---

## Task 6.4 — Retrieval API

Embedded Mode：

```python
aer.retrieve(
    task="WordPress REST API 403",
    domain="wordpress"
)
```

返回：

```python
list[Experience]
```

---

## Task 6.5 — Context Formatter

将 Experience 转为 Agent 可使用的简洁文本：

```text
Historical verified experience:

Problem:
...

Root cause:
...

Recommended action:
...

Avoid:
...

Historical success rate:
...
```

禁止将完整 Run Raw Logs 注入 Agent。

---

# 10. Milestone 7 — Experience Usage（已完成）

## Goal

证明 Experience 到底有没有价值：一条经验被检索之后，是否真正进入 Agent 上下文、
Agent 是否采用、任务结果如何，以及它是否值得继续信任。

本轮的关键不是"加统计"，而是**拒绝错误归因**。四个区别必须进入数据模型：

```text
Retrieved ≠ Injected ≠ Adopted ≠ Helpful
Task SUCCESS ≠ Experience caused success
```

---

## Task 7.1 — retrieval_sessions

一次检索一行，**0 结果也要写**（"查过但没有"≠"根本没查"）。

```text
id / run_id(nullable) / query_text(sanitized) / query_fingerprint / domain /
mode / requested_limit / result_count / knowledge_projection_version /
retrieval_policy_version / retrieval_duration_ms / experiment_id(nullable) /
assignment / created_at / metadata_json
```

`query_text` 只存 sanitized 检索问题（redact + 截断），不存原始 prompt /
conversation / tool output。`query_fingerprint` 用于统计同类检索。

`experiment_id` / `assignment`(NONE/TREATMENT/HOLDOUT) 为将来的 holdout 预留，
本轮不实现实验框架。

---

## Task 7.2 — experience_usage 与 Injection Tracking

一行 = 一次 retrieval session × 一条 experience，数据库 UNIQUE 保证同一次检索中
同一条经验最多出现一次。

```text
id / retrieval_session_id / experience_id / rank / role / retrieval_score /
retrieved_at / injected_at(nullable) / injection_position / injection_chars /
context_fingerprint / formatter_version / usage_signal + source /
utility_label + source / created_at / updated_at / metadata_json
```

```text
role 保存检索当时的角色（GUIDANCE / WARNING / OBSERVATION），
     不根据 ExperienceKind 事后重算——检索策略将来会变。
rank / retrieval_score 保存检索当时的值，不用今天的 Ranker 重算历史。
```

**Injection 必须是结果的子集**：session 返回 A/B/C 时只能记录其中一部分，
不能记录根本不在结果里的 D（FK + 应用层双重校验）。

**不保存完整 Injected Prompt**：只存 experience ids / position / char count /
formatter version / context fingerprint。避免重复数据、隐私泄漏、把注入内容永久化。

---

## Task 7.3 — UsageSignal / Utility

```text
usage_signal  UNKNOWN / ADOPTED / IGNORED / REJECTED    + source
utility_label UNKNOWN / HELPFUL / NEUTRAL / HARMFUL     + source
```

```text
source：AGENT / HUMAN / ADAPTER / EVALUATOR / SYSTEM
```

**禁止自动行为推断**：不许因为 Agent 后来调用了 `update_page`、而经验里也有
`update_page`，就把 UNKNOWN 改成 ADOPTED。推断最多留给将来带置信度的
`inferred adoption`。

转换规则：

```text
UNKNOWN → 任何值        允许
相同值重复写入          幂等
已确定值 → 其他值        拒绝，除非显式 override=True
```

**ADOPTED ≠ HELPFUL**：Agent 完全可能采用了一条错误的经验。

---

## Task 7.4 — Effectiveness 统计

```python
report = aer.experience_effectiveness(experience_id)
```

```text
retrieval_count / injection_count
explicit_adoption_count / explicit_ignore_count / explicit_rejection_count
helpful_count / neutral_count / harmful_count
distinct_target_runs
verified_success_runs / verified_failure_runs / run_failed_runs /
unverified_runs / running_runs
unattributed_usage_count
observed_success_rate / injected_verified_success_rate /
adopted_verified_success_rate
```

```text
1. 计数单位是「不同的 run」，不是 usage 行。
2. 分母只包含「已 Injected 且 target run 最终有 Verification」的 run。
   UNVERIFIED 不算 Failure，也不进分母。
3. adoption 子集单独报告，不与 injected 口径混合。
```

名字必须是 `observed_success_rate`：这是观察数据，不是因果效果。

**Outcome 不重复落库**：不在 usage 表里存 `task_success`，任务结果在 Run +
Verification，统计时 JOIN 派生，避免 Run 变了而 usage 里的副本过期。

---

## Task 7.5 — Promotion（REUSED / PROVEN）

```text
VERIFIED → REUSED   被注入到与 source run 不同的真实 Run
                    （仅被检索不算；被自己的 source run 注入也不算；不要求成功）
REUSED  → PROVEN    全部阈值可配置（PromotionPolicy）：
                      distinct adopted runs        >= 5
                      adopted verified successes   >= 4
                      adopted success rate         >= 0.80
                      harmful feedback             == 0
```

只有 Injected 而全部 UNKNOWN：**可以 REUSED，默认不得自动 PROVEN**。

`PROVEN` 仍然不是 causally proven，本节所有阈值都是配置项。

---

## Task 7.6 — Confidence

```text
confidence = 0.30 * verification + 0.25 * reuse + 0.25 * outcome
           + 0.10 * feedback     + 0.10 * freshness
```

确定、可拆解、按需计算、**不写回**。同样的证据 + 同样的时钟 => 同样的数值。
禁止 LLM 决定 confidence。

---

## Task 7.7 — 本轮明确不做

```text
HNSW / Embedding / Vector Retrieval
Agent Adapter / Codex / Claude / Cursor / DSH Adapter（接口给 M8 留着）
Dataset Builder / Preference Dataset / SFT Export / Training
Workflow Promotion / A/B Experiment Framework / Dashboard / FastAPI
Usage 投影进 NeuG
自动行为推断 adoption
```

---

# 10.1 Milestone 8 — Agent Adapter Protocol（已完成）

## Goal

让 Codex / Claude Code / Cursor / DSH / 内部 Agent 用**同一套协议**接入 AER，
而 AER Core 完全不需要认识任何一种私有日志格式（round-8 brief，第 1 节）。

本轮**只做**：

```text
Agent Adapter Protocol
+ Reference Adapter SDK（GenericAgentAdapter）
+ Fake / Generic Adapter（测试用两个不同风格的假 Agent）
```

本轮**不做**任何真实厂商 Adapter（属于 M8.1–M8.4）。

---

## Task 8.1 — 分层边界

```text
Codex / Claude Code / Cursor / DSH / Internal Agent
        │
        ▼  厂商专属翻译（在 AER 之外）
Agent-specific Adapter
        │
        ▼  本协议
Agent Adapter Protocol
        │
        ▼
AER Runtime
```

Adapter 只做：**Translate / Normalize / Sanitize / Associate**。
Adapter 不做：Distillation、Verification Policy、Ranking、Promotion、Dataset。

**Adapter 不得直接访问 Repository**，只能走 Runtime API（否则会绕过
terminal-state guard、单一错误管道与 sequence 分配）。

---

## Task 8.2 — 协议词汇

```text
AER_ADAPTER_PROTOCOL_VERSION = "1"   # 与包版本无关，独立命名（第 35 节）
AgentIdentity            provider / agent_name / agent_version / model /
                         model_version / adapter_name / adapter_version /
                         session_id / external_run_id
AdapterCapabilities      tool_events / explicit_adoption_signal / human_feedback /
                         decision_summary / external_verification / session_linkage /
                         explicit_utility_signal（默认全部 false）
AgentAction              kind / name / tool_name / input_summary
AgentObservation         kind(TOOL_SUCCESS|TOOL_FAILURE|ENVIRONMENT|HUMAN) /
                         summary / detail
AgentExecutionEnvelope   event_type / identity / protocol_version /
                         external_event_id / external_session_id / external_run_id /
                         external_timestamp / external_sequence / action /
                         observation / payload / metadata
AdapterSessionRequest    external_session_id / task / task_type / external_run_id
AdapterFinishRequest     status(nullable) / reason / metadata
AgentAdapter(Protocol)   name / protocol_version / identity / capabilities /
                         start / handle_event / finish
```

```text
不要依赖 model 名称判断 Agent 类型（第 3 节）。
厂商时间戳只作为 external metadata；AER 的 created_at 永远是接收时钟（第 24 节）。
外部 sequence 原值保留，绝不重写 AER sequence（第 23 节）。
```

## Task 8.3 — 事件词汇

不新增 EventType。Adapter 事件只能映射到已有语义：

```text
MODEL_CALL / MODEL_RESULT / TOOL_CALL / TOOL_RESULT /
ERROR / RECOVERY_START / RECOVERY_RESULT / HUMAN_FEEDBACK
```

明确**不可**通过协议写入：

```text
TASK_START / TASK_END   由 open / close session 驱动
VERIFICATION            只能由 Verification Engine 写入（第 39 节）
```

`decision_summary` 承载可审计的行动理由摘要；`chain_of_thought` /
`reasoning` / `scratchpad` 等私有推理字段一律**丢弃并记录**（第 6 节）。

## Task 8.4 — 会话

新增 `adapter_sessions`：`provider + external_session_id` 唯一 → 一个当前 AER Run。

```text
首次连接              → STARTED，新建 Run
重连（Run 仍 RUNNING） → RESUMED，复用同一个 Run
重连（Run 已终态）     → 默认拒绝（AdapterSessionTerminated）
                         显式 reopen() → REOPENED，新 Run，旧 run id 进 previous_runs
```

映射持久化，进程重启后仍可恢复（第 50–51 节）。

## Task 8.5 — 幂等

新增 `adapter_events` 账本：`(provider, external_event_id)` 唯一。

```text
一次厂商事件 = 一个幂等单元
（一个厂商事件拆成多个 envelope 时，必须携带同一个 external_event_id）

先 claim（applied=false）→ 再 apply → 再 mark_applied
重复投递 = DUPLICATE，不写任何东西
没有 external_event_id = 无法幂等（这是文档化的代价，不是猜测）
```

## Task 8.6 — 外部输入治理

Adapter 输入全部视为 **Untrusted External Data**（第 25–27 节）：

```text
凭据按值脱敏（Bearer / key=value / URL / PEM / SSH）
凭据按键名脱敏（api_key / authorization / cookie / token ...）
私有推理键直接丢弃（不是脱敏）
单字符串上限 4096；decision_summary 上限 2048；整体 body 上限 16384
超限一律带显式截断标记，禁止静默截断
深度上限 8，超出替换为显式 marker
```

Prompt injection 不靠匹配处理，而是**结构上不可达**：协议里没有任何字段
能让外部字符串变成 AER 指令（第 26 节）。

## Task 8.7 — 能力声明

```text
Adapter 声明能观测什么；Runtime 按声明**强制**：
  tool_events              → 才能写 TOOL_CALL / TOOL_RESULT
  human_feedback           → 才能写 HUMAN_FEEDBACK
  explicit_adoption_signal → 才能写任何非 UNKNOWN 的 usage_signal
  explicit_utility_signal  → 才能写 utility_label
  external_verification    → 才能提交证据给 verifier
```

不支持 adoption 的 Adapter 永远只能留下 `UNKNOWN`，**不得推断 ADOPTED**（第 12、47 节）。
Adapter **不得**因为任务成功就写 `HELPFUL`（第 16、34 节）。

## Task 8.8 — 错误语义

```text
Agent 自己报错            → ERROR 事件 + ErrorRecord（沿用唯一错误管道）
Adapter 自己崩溃          → AdapterError（Integration Error）
                           不改 Run 状态、不写 ERROR 事件、不伪造 Verification
                           因为那会污染 Distillation 的证据（第 28、48 节）
```

## Task 8.9 — 本轮明确不做

```text
Codex / Claude / Cursor / DSH Adapter（M8.1–M8.4，按 hook 完整度排序而非品牌）
Dataset Export / Preference Dataset / 训练
Dashboard / FastAPI
Plugin Marketplace（注册表就是一个 dict）
重写 M1–M4 的 Event 语义（Adapter 适配 AER，不是反过来）
```

---

# 11. Milestone 8（原编号，后续执行）— Sanitizer

> 编号说明：本文件原先把 Sanitizer 编为 M8。本轮（round-8）的 M8 是
> Agent Adapter Protocol，Sanitizer 保留原任务内容但顺延，待真实多 Agent
> 接入后按需要排期。Adapter 层已经自带一层外部输入治理（见 Task 8.6）。

## Goal

确保 Agent Trace 不会把 Credential 长期保存。

Credential：

> 身份凭证，例如密码、Token、API Key。

---

## Task 8.1 — Sanitizer Middleware

所有数据：

```text
Agent
↓
Sanitizer
↓
Storage
```

---

## Task 8.2 — Default Secrets

至少匹配：

```text
Authorization
Bearer Token
API Key
Password
Cookie
SSH Private Key
Database URL
Secret
Access Token
Refresh Token
```

替换为：

```text
[REDACTED]
```

---

## Task 8.3 — Test Sanitizer

测试：

```text
Bearer sk-xxxx
```

数据库中不得出现原值。

---

# 12. Milestone 9 — Artifact Storage

## Goal

避免 SQLite 被大型文件撑大。

---

## Task 9.1 — Artifact Manager

保存：

```text
Screenshot
HTML
Log
JSON
CSV
Report
```

到：

```text
data/runs/{run_id}/
```

---

## Task 9.2 — Hash

每个 Artifact 保存：

```text
SHA-256
```

用于：

```text
完整性验证
重复文件识别
```

---

# 13. Milestone 10 — Embedded Mode

## Goal

AER 第一版不依赖独立服务器。

目标：

```python
from aer import AER

aer = AER(".aer")
```

直接可运行。

---

## Task 10.1 — Init

支持：

```bash
aer init
```

生成：

```text
.aer/
├── aer.db
├── artifacts/
├── datasets/
├── backups/
└── config.yaml
```

---

## Task 10.2 — No-server Usage

以下代码必须可运行：

```python
aer = AER(".aer")

with aer.run("test task") as run:
    ...
```

整个过程不得要求：

```text
Redis
HTTP Server
External Database
```

---

# 14. Milestone 11 — CLI

## Goal

提供最小可用命令行。

CLI：

> Command-Line Interface，命令行界面。

---

## Task 11.1 — Core Commands

实现：

```bash
aer init

aer runs

aer run show <id>

aer experience list

aer experience show <id>

aer experience search "query"

aer stats
```

---

## Task 11.2 — Debug Command

建议：

```bash
aer doctor
```

检测：

```text
SQLite
Migration
Directory Permission
Database Integrity
FTS5
Configuration
```

---

# 15. Milestone 12 — Minimal API

只有 Embedded Mode 稳定以后实现。

---

## Task 12.1 — FastAPI

提供：

```text
GET /health

GET /runs
GET /runs/{id}

GET /experiences
GET /experiences/{id}

POST /experiences/search
```

不要第一阶段暴露复杂写 API。

---

# 16. Milestone 13 — Minimal Dashboard

只有核心闭环稳定以后开发。

页面：

```text
/runs
/runs/:id
/experiences
/experiences/:id
/failures
/metrics
```

---

## Run Detail

显示 Timeline：

```text
Task Start

Model Call

Tool Call

Error

Recovery

Verification

Task End
```

---

# 17. Real-world Experiment

最终必须使用真实 WP Agent 测试。

---

## Experiment A — Without Experience

关闭 Experience Retrieval。

运行：

```text
至少 50 个任务
```

记录：

```text
Success Rate
First Pass Success
Retry
Tool Call
Execution Time
Token
Human Intervention
```

---

## Experiment B — With Experience

打开 Retrieval。

运行类似：

```text
至少 50 个任务
```

记录同样指标。

---

# 18. Key Metrics

核心指标：

```text
Task Success Rate
任务成功率

First Pass Success Rate
首次成功率

Recovery Rate
故障恢复率

Experience Hit Rate
经验命中率

Experience Assisted Success Rate
经验辅助任务成功率

Average Retry Count
平均重试次数

Average Tool Calls
平均工具调用数

Average Execution Time
平均执行耗时

Human Intervention Rate
人工介入率
```

---

# 19. Main MVP Hypothesis

最终要验证：

```text
P(success | experience)
>
P(success | no experience)
```

即：

> 使用经过验证的历史经验后，Agent 的任务成功概率是否显著提高。

如果不能证明：

不要开始模型训练。

优先修复：

```text
Experience Quality
Retrieval
Verification
Distillation
```

---

# 20. Test Requirements

至少建立：

```text
tests/runtime/
tests/storage/
tests/verification/
tests/experience/
tests/retrieval/
tests/security/
```

---

## Mandatory Tests

必须覆盖：

```text
Run creation

Run completion

Event ordering

Tool exception capture

SQLite restart persistence

Verification failure

Experience lifecycle

Experience deduplication

FTS retrieval

Experience usage

Confidence calculation

Secret sanitization
```

---

# 21. Code Quality Gate

合并代码前至少运行：

```bash
pytest
```

建议同时：

```bash
ruff check .
mypy aer/
```

其中：

`ruff`

> Python 静态代码检查工具。

`mypy`

> Python 类型检查工具。

---

# 22. Git Commit Strategy

建议 Commit 按模块拆分。

例如：

```text
feat(runtime): add run lifecycle

feat(storage): add sqlite repositories

feat(hooks): capture tool execution

feat(verification): add deterministic verifier

feat(experience): add experience distiller

feat(retrieval): add sqlite fts search

test(runtime): add lifecycle tests
```

不要一次提交：

```text
implement whole AER
```

---

# 23. Forbidden Work During P0

P0 开发期间禁止主动实现：

```text
Model Fine-tuning

QLoRA

SFT

DPO

RL

Agent Lightning Integration

Kafka

Redis

PostgreSQL

Microservices

Kubernetes

Multi-tenant Billing

Complex User System

Huge Dashboard

Independent Vector Database
```

这些全部属于后续阶段。

---

# 24. P0 Completion Checklist

必须全部满足：

```text
[ ] AER 可以 Embedded Mode 启动

[ ] SQLite 自动初始化

[ ] Run 可以创建

[ ] Event 可以记录

[ ] Tool Call 自动 Trace

[ ] Error 自动记录

[ ] Recovery 自动记录

[ ] Verifier 可以验证结果

[ ] Experience 可以生成

[ ] Experience 可以持久化

[ ] Experience 可以全文搜索

[ ] Experience 可以注入新任务

[ ] Experience Usage 可以统计

[ ] Secret 自动脱敏

[ ] 所有核心测试通过
```

---

# 25. Final MVP Acceptance Test

必须真实执行以下场景：

```text
Run A
```

任务：

```text
WordPress 页面修改
```

过程中出现真实或模拟故障：

```text
REST API 403
```

Agent：

```text
第一次操作失败
↓
诊断权限
↓
修复
↓
再次调用
↓
成功
```

Verifier 验证：

```text
HTTP 200
目标 DOM 正确
修改实际生效
```

AER 自动：

```text
提炼 Experience
```

然后执行：

```text
Run B
```

遇到相似：

```text
REST API 403
```

AER：

```text
检索历史 Experience
↓
注入 Agent
```

Agent：

```text
直接优先检查 Credential / Capability
```

而不是重复无意义 Retry。

最终：

```text
Verifier PASS
```

并记录：

```text
Experience A
reuse_count += 1

success_count += 1
```

这个场景连续稳定通过后：

# P0 MVP Accepted

---

# 26. Milestone 6 — NeuG Knowledge Index & Retrieval（已完成）

完成日期：2026-09-18。验收详见 `docs/DEPLOYMENT.md` 第 17 节与
`docs/DECISIONS.md` D-053..D-063。

```text
[✓] SQLite 仍是唯一事实源，NeuG 是可重建投影（D-053）
[✓] 引擎行为以目标环境实测为准，不以文档为准（D-054，7 条与文档不符）
[✓] neug 精确锁定 0.2.0（D-055）
[✓] 只做 BM25 + 图过滤，不做 HNSW（D-056）
[✓] 跨库不做分布式事务：投影最终可修复（D-057）
[✓] 知识库可无限重建，rebuild 是唯一修复手段（D-058）
[✓] FAILURE 只进 warnings（D-059）
[✓] 检索只读，不记 usage（D-060）
[✓] 嵌入式优先于 Service Mode（D-061）
[✓] 用户查询必须转成安全的全文表达式（D-062）
[✓] 投影按批提交（D-063）
```

---

# 26.1 Milestone 7 — Experience Usage & Effectiveness（已完成）

完成日期：2026-09-20。验收详见 `docs/DEPLOYMENT.md` 第 19 节与
`docs/DECISIONS.md` D-066..D-074。

```text
[✓] Usage 存 SQLite，不进 NeuG；NeuG 本轮保持只读（D-066）
[✓] retrieve() 保持纯读，新增显式 tracked retrieval（D-067）
[✓] Retrieved / Injected / Adopted / Helpful 四态分离（D-068）
[✓] usage_signal 必须显式 + 带来源，禁止行为推断（D-069）
[✓] REUSED 要求注入到非来源 Run；来源 Run 不算 reuse（D-070）
[✓] observed_success_rate 命名即边界：相关性不是因果（D-071）
[✓] confidence 确定性、可拆解、按需计算、不写回（D-072）
[✓] 0 结果也记录 session；query 必须 sanitized（D-073）
[✓] 不建 Dataset Builder、不做训练、不做 Adapter（D-074）
```

---

# 26.2 Milestone 8 — Agent Adapter Protocol（已完成）

完成日期：2026-09-20。验收详见 `docs/DEPLOYMENT.md` 第 23 节与
`docs/DECISIONS.md` D-075..D-083。

```text
[✓] Core 不认识任何厂商格式；Adapter 只做 Translate/Normalize/Sanitize/Associate（D-075）
[✓] Adapter 只能走 Runtime API，新增 RunContext.external（D-076）
[✓] 事件型模型，不做 call/result 配对；外部顺序与时间戳只作元数据（D-077）
[✓] AER 自己生成内部 ID；外部 ID 永不作为主键（D-078）
[✓] 幂等靠 adapter_events 账本，先 claim 后 apply（D-079）
[✓] 外部输入全部不可信：按值/按键脱敏 + 尺寸上限 + 显式截断（D-080）
[✓] 能力声明被强制执行：不能观测就只能是 UNKNOWN（D-081）
[✓] Adapter 崩溃是 Integration Error，不改 Run、不伪造 Verification（D-082）
[✓] 会话映射持久化，terminal 语义显式（D-083）
[✓] 两个完全不同的假 Agent 产生相同 AER 语义（第 60 节验收）
```

# 26.3 Next Stage Gate

M8 之后建议顺序：

```text
M8.1 Codex Adapter
M8.2 Claude Code Adapter
M8.3 Cursor Adapter
M8.4 DSH Adapter
```

顺序应按**哪个平台能提供最完整可靠的 hooks**决定，不按品牌优先级（第 57 节）。

**不要**从 M8 直接进入 Dataset Builder：本轮只是让多个 Agent 能稳定产生
**统一 Evidence**；Dataset 还需要 Quality Gate、Safety、Dedup 与
Holdout separation（第 56 节）。

---

# 27. Next Stage Gate

只有 P0 MVP Accepted 后，才能进入：

```text
P1
```

P1 才考虑：

```text
Embedding
Hybrid Retrieval
Dashboard
Workflow Promotion
Advanced Metrics
Experience Graph
```

再之后才是：

```text
P2
```

包括：

```text
Dataset Builder
Model Training
Small Model
SFT
DPO
RL
Agent Lightning
```

---

# 27. Agent Instruction

编码 Agent 在处理本项目时：

1. 先阅读 `agent.md` 和 `TASKS.md`。
2. 不主动扩展 MVP 范围。
3. 修改架构前说明必要性。
4. 优先完成当前 Milestone。
5. 每个新模块必须有测试。
6. 不能跳过 Repository 直接访问数据库。
7. 不允许 Agent 自评替代 Verifier。
8. 不允许未经验证的 Experience 进入训练数据。
9. 所有长期数据写入前必须经过 Sanitizer。
10. 遇到复杂设计时优先选择更简单且可验证的方案。

---

# 28. Final Rule

任何开发任务开始前，先判断：

> 这个功能是否直接帮助 AER 完成“执行 → 验证 → 经验 → 检索 → 再执行”的闭环？

如果不能：

**不要在当前 MVP 实现。**
---

# 10.2 Milestone 8.1 — Codex Adapter（已完成）

完成日期：2026-09-20。验收详见 `docs/CODEX_ADAPTER.md` 与
`docs/DECISIONS.md` D-085..D-094。

```text
[✓] 先侦察：读实际安装的 0.155.1，不照文档假设（D-090）
[✓] hooks 而非 notify；notify 只是 turn 级 fallback（D-085、D-086）
[✓] 唯一 terminal authority：SessionEnd；Stop 不终止 Run（D-087）
[✓] SessionStart 无 task 字段 → Run 由第一个 prompt 建立（D-091）
[✓] 只映射被观测到的两个事件，其余显式 IGNORED（D-092）
[✓] 未声明结果的 SessionEnd → ABORTED，并记录 codex_outcome_stated（D-093）
[✓] Codex 自述成功不产生 Verification（D-088）
[✓] 工具成功不等于 adoption；能力门禁拒绝 ADOPTED（D-089）
[✓] 幂等：同一 hook 重复投递只写一条（D-079 + 实测 hook 重复触发问题）
[✓] crash gap：可检测（unapplied_events），不声称 replay（D-094）
[✓] 真实 payload fixture（3 个捕获）+ contract fixture（3 个，明确标注）
[✓] scripts/probe_codex_hooks.py：升级 Codex 前的可重复兼容性探针
```

**未完成并明确记录的缺口**：PreToolUse / PostToolUse / PermissionRequest /
PreCompact / PostCompact / SubagentStart / SubagentStop / Stop / Interrupt
九个事件在 exec 模式下未观测到（需要一次真实模型回合）；interactive 模式全部未覆盖
（需要终端）。详见 `docs/CODEX_ADAPTER.md` 的覆盖矩阵。

---

# 10.3 Milestone 8.1 — Real Codex Tool Coverage（**COMPLETE**）

完成日期：2026-09-20。决策见 `docs/DECISIONS.md` D-095..D-099。

前置条件（provider 可达）在本轮达成：重新登录后 access token 有效（240h），
用 `gpt-5.6-luna` 在临时 CODEX_HOME + 临时测试仓库中完成了真实模型回合。

```text
[✓] SessionStart      CAPTURED
[✓] UserPromptSubmit  CAPTURED
[✓] PreToolUse        CAPTURED   （tool_name / tool_input / tool_use_id）
[✓] PostToolUse       CAPTURED   （tool_response 是字符串，无结果字段）
[✓] SessionEnd        CAPTURED   （reason="other"，正常值）
[✓] Stop              CAPTURED   （turn 级，带 last_assistant_message，无结果）
```

真实场景 B（失败 → 修复 → 成功）完整捕获：

```text
SessionStart → UserPromptSubmit →
PreToolUse(Bash, python check.py) → PostToolUse(失败输出) →
PreToolUse(Bash, 查看文件)         → PostToolUse →
PreToolUse(apply_patch, 修改)      → PostToolUse →
PreToolUse(Bash, 复跑)             → PostToolUse(成功输出) →
Stop → SessionEnd
```

本轮**修正的映射**（依据真实 payload，§6/§7）：

```text
[✓] PreToolUse ↔ PostToolUse 通过 tool_use_id 1:1 关联 —— linkage available（§8）
[✓] tool_response 是字符串：删除顶层 exit_code/error/status 检查（死代码）
[✓] 工具失败不写 ERROR（唯一信号是输出文本，解析文本被 §52 禁止）—— D-097
[✓] Stop 不终止 Run（turn 级且无结果）—— D-098
[✓] SessionEnd.reason="other" 是正常值 —— P0 达成，D-093 的保护被证实必需 —— D-099
[✓] 3 个 contract fixture 全部删除，9 个 captured fixture 入库（MANIFEST 全部标记 captured）
[✓] fixture 已脱敏：个人绝对路径归一化、transcript 文件名归一化、id 替换为合成值
```

**遗留限制**：PermissionRequest / PreCompact / PostCompact / SubagentStart / SubagentStop /
Interrupt 仍未观测；interactive 模式零覆盖；`verified_success` 在当前映射下对 Codex Run
不可达（因为 Codex 从不声明结果）。


---

# 10.4 Milestone 8.1.1 — Outcome Semantics（已完成）

完成日期：2026-09-20。决策见 `docs/DECISIONS.md` D-100。范围只有一件事：
把"平台没说结果"变成一个 AER 能表达的事实，而不是借用一个错误的状态。

```text
[✓] RunStatus.INCONCLUSIVE（终态，不声明任何关于工作的东西）
[✓] Codex SessionEnd(未声明) → INCONCLUSIVE；generic 增加 unknown/inconclusive 词
[✓] is_verified_success：SUCCESS 或 INCONCLUSIVE + 有 required + 无 required 失败
[✓] Distillation：INCONCLUSIVE + 已验证 → SUCCESS/RECOVERY
                  INCONCLUSIVE + 未验证 → 没有 kind，veto（不伪造 FAILURE）
[✓] 旧语义不变：FAILED / ABORTED / PARTIAL_SUCCESS 仍然永远不是 verified success
[✓] 删除 metadata.outcome_declared（状态即事实，撤掉第二份表示）
```

验收（对照本轮 brief 的 Final Acceptance）：

```text
[✓] INCONCLUSIVE terminal status
[✓] Codex SessionEnd(other) → INCONCLUSIVE
[✓] Verification Summary 支持 INCONCLUSIVE + required PASS → verified_success
[✓] Distillation 正确处理（已验证 → 有 kind；未验证 → 不编造 kind）
[✓] 旧 SUCCESS / FAILURE / ABORTED 语义不变（有回归测试）
```

新增测试：summary 4 条、policy 6 条、lifecycle 3 条、effectiveness 2 条、
Codex 端到端验收 5 条、generic 词表 2 条。

**这一步的意义**：第二个平台只需要回答 `declared SUCCESS / FAILURE / ABORTED / UNKNOWN`
四个词，"UNKNOWN + verifier PASS 算不算成功"由 AER 统一决定一次。
M8 的 provider-neutral 闭环因此成立：

```text
Agent execution → Declared Outcome or INCONCLUSIVE → Independent Verification
              → Verified Outcome → Experience → Retrieval → Usage
```

**仍然存在的限制**：Codex 在没有验证路径时产不出经验（因为没有 kind 可写）。
这是刻意的选择，见 D-100 的"代价"。
