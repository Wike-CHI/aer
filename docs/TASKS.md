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

# 10. Milestone 7 — Experience Usage

## Goal

证明 Experience 到底有没有价值。

---

## Task 7.1 — experience_usage

记录：

```text
experience_id
run_id
retrieved
injected
useful
task_success
```

---

## Task 7.2 — Injection Tracking

区分：

```text
Retrieved
```

和：

```text
Injected
```

检索出来但没有放入 Agent Context 的 Experience 不得算实际使用。

---

## Task 7.3 — Success Statistics

统计：

```text
reuse_count
success_count
failure_count
success_rate
```

计算：

```text
success_rate =
success_count / reuse_count
```

注意处理：

```text
reuse_count = 0
```

---

## Task 7.4 — Experience Promotion

建议默认规则：

### VERIFIED → REUSED

```text
至少被其他 Run 实际注入一次
```

### REUSED → PROVEN

```text
reuse_count >= 5
success_rate >= 0.80
```

### PROVEN → TRAINING_CANDIDATE

```text
reuse_count >= 10
success_rate >= 0.90
verification complete
```

全部做成配置项。

---

## Task 7.5 — Confidence Calculation

禁止直接使用 LLM 输出作为最终 Confidence。

建议：

```python
confidence = (
    verification_score * 0.30
    + reuse_score * 0.25
    + success_rate * 0.25
    + human_score * 0.10
    + freshness_score * 0.10
)
```

第一版允许根据数据情况调整。

---

# 11. Milestone 8 — Sanitizer

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

# 26. Next Stage Gate

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