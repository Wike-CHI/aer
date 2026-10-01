# AER Agent Development Guide

## 1. Project Overview

项目名称：

```text
AER
Agent Experience Runtime
智能体经验运行时
```

AER 是一个面向 Agent 系统的轻量级经验学习基础设施。

它的目标不是构建一个新的通用 Agent，也不是让模型实时修改自身权重，而是：

> 将 Agent 在真实任务中的执行、失败、恢复、验证和成功过程转化为可追踪、可验证、可检索、可复用的经验。

核心闭环：

```text
Task
↓
Agent Execution
↓
Hook Capture
↓
Trajectory
↓
Verification
↓
Experience Distillation
↓
Experience Store
↓
Retrieval
↓
Next Agent Execution
```

第一阶段重点是：

```text
Experience Retrieval
```

而不是：

```text
Model Training
```

---

# 2. Core Principle

整个项目必须遵循：

```text
Experience First
Training Later
```

即：

> 先证明历史经验可以提高 Agent 的实际任务成功率，再考虑训练模型。

禁止为了“AI 自我进化”而提前增加复杂训练架构。

---

# 3. MVP Goal

AER MVP 必须完成以下闭环：

```text
Agent 执行任务

↓

Hook 自动记录过程

↓

SQLite 保存执行轨迹

↓

Verifier 验证执行结果

↓

失败 / 恢复 / 高价值任务被提炼为 Experience

↓

Experience 保存

↓

后续相似任务自动检索 Experience

↓

Experience 注入 Agent 上下文

↓

记录 Experience 是否真正帮助任务完成
```

MVP 成功标准：

```text
使用 Experience 的任务

在成功率、重试次数、执行时间、
Token 消耗或人工修改率方面

优于

未使用 Experience 的任务
```

---

# 4. Scope

## 4.1 P0

必须优先实现：

```text
Runtime
Hook
Run
Event
SQLite
Error
Recovery
Verification
Experience
Retrieval
```

## 4.2 P1

P0 稳定后实现：

```text
Experience Usage
Confidence
Embedding
Hybrid Retrieval
Workflow
Dashboard
```

## 4.3 P2

暂不实现：

```text
Fine-tuning
SFT
DPO
RL
Distributed Runtime
Kafka
Redis
PostgreSQL
独立 Vector Database
Multi-Tenant
复杂 RBAC
```

除非后续出现明确业务需求。

---

# 5. Architecture Principle

项目坚持：

```text
单机优先
SQLite 优先
嵌入式优先
同步优先
确定性代码优先
外部验证优先
低依赖优先
可替换优先
```

不要为了所谓“企业级”提前引入分布式架构。

---

# 6. Technology Stack

默认技术栈：

```text
Language
Python 3.12+

Backend
FastAPI

Schema Validation
Pydantic

ORM
SQLAlchemy 2.x

Database
SQLite

Migration
Alembic

Agent Framework
LangGraph optional

Frontend
React optional

Testing
pytest
```

MVP 最早阶段可以不启动 FastAPI。

优先支持：

```text
Embedded Mode
```

即 AER 直接作为 Python Library 嵌入 Agent。

---

# 7. SQLite Configuration

初始化 SQLite 时必须执行：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

解释：

- `WAL`：Write-Ahead Logging，预写式日志，提高读写并发能力。
- `foreign_keys`：启用外键约束。
- `synchronous=NORMAL`：在安全与性能之间取得平衡。
- `busy_timeout`：数据库被短暂占用时等待，而不是立即失败。

---

# 8. Project Structure

推荐目录：

```text
aer-runtime/
│
├── aer/
│   ├── runtime/
│   │   ├── run.py
│   │   ├── event.py
│   │   └── hook.py
│   │
│   ├── storage/
│   │   ├── database.py
│   │   ├── repositories.py
│   │   └── files.py
│   │
│   ├── verification/
│   │   ├── base.py
│   │   ├── deterministic.py
│   │   ├── environment.py
│   │   ├── llm.py
│   │   └── human.py
│   │
│   ├── experience/
│   │   ├── models.py
│   │   ├── distiller.py
│   │   ├── promoter.py
│   │   ├── confidence.py
│   │   └── usage.py
│   │
│   ├── retrieval/
│   │   ├── keyword.py
│   │   ├── semantic.py
│   │   ├── hybrid.py
│   │   └── ranker.py
│   │
│   ├── dataset/
│   │   ├── builder.py
│   │   └── exporter.py
│   │
│   ├── api/
│   ├── schemas/
│   ├── models/
│   └── sdk/
│
├── data/
│   ├── aer.db
│   ├── runs/
│   ├── datasets/
│   └── backups/
│
├── migrations/
├── examples/
│   └── wordpress-agent/
│
├── tests/
├── docs/
└── pyproject.toml
```

不要随意增加新的顶层目录。

---

# 9. Core Domain Objects

系统中的核心对象：

```text
Run
Event
Artifact
Error
Verification
Experience
ExperienceSource
ExperienceUsage
Workflow
DatasetItem
```

Agent 开发时，应优先围绕这些对象扩展，不要创造大量语义重复的数据结构。

---

# 10. Run

`Run` 表示一次完整 Agent 任务执行。

例如：

```text
修改某个 WordPress 产品页 H1
```

就是一个 Run。

Run 必须至少包含：

```text
id
task_type
task_description
agent_name
agent_version
model_provider
model_name
status
started_at
ended_at
final_score
metadata
```

状态只允许：

```text
RUNNING
SUCCESS
PARTIAL_SUCCESS
FAILED
ABORTED
```

禁止动态创建新的 Run Status。

---

# 11. Event

Event：

> Agent 执行过程中产生的一次标准化事件。

允许的核心 Event Type：

```text
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

每个 Event 必须包含：

```text
run_id
sequence
event_type
created_at
```

推荐包含：

```text
input
output
duration_ms
metadata
```

---

# 12. Hook

Hook：

> 当 Agent 生命周期中的某个事件发生时自动执行的回调机制。

核心 Hook：

```python
on_task_start()
on_model_call()
on_model_result()
on_tool_start()
on_tool_result()
on_step_error()
on_recovery()
on_verification()
on_human_feedback()
on_task_end()
```

业务 Agent 不应该直接操作数据库。

正确方式：

```text
Business Agent
↓
AER SDK
↓
Hook
↓
Repository
↓
SQLite
```

---

# 13. SDK Design

Agent 接入应该尽可能简单。

目标使用方式：

```python
from aer import AER

aer = AER("./data")

run = aer.start_run(
    task="修复 WordPress 页面 H1"
)
```

工具调用推荐：

```python
with run.tool("wordpress.update_page") as event:
    result = update_page()
    event.set_result(result)
```

异常：

```python
try:
    ...
except Exception as exc:
    run.error(exc)
```

恢复：

```python
with run.recovery(reason="WordPress REST API returned 403"):
    ...
```

验证：

```python
run.verify(
    name="h1_exists",
    passed=len(h1_nodes) == 1
)
```

结束：

```python
run.success()
```

---

# 13.1 Agent Adapter Protocol（M8）

当 Agent 不是我们自己写的、而是 Codex / Claude Code / Cursor / DSH 这类平台时，
接入方式不是 SDK，而是**协议**：

```text
Codex / Claude Code / Cursor / DSH / Internal Agent
        │
        ▼  厂商专属翻译（在 AER 之外，M8.1–M8.4）
Agent-specific Adapter
        │
        ▼  Agent Adapter Protocol
        ▼
AER Runtime
```

一条规则：**AER Core 永远不认识任何厂商格式**。Adapter 只做四件事——

```text
Translate   厂商事件 → AER 标准事件
Normalize   统一 AgentAction / AgentObservation 形状
Sanitize    外部输入按不可信数据处理
Associate   把外部 session / run 关联到 AER Run
```

Adapter **不做**：Distillation、Verification Policy、Ranking、Promotion、Dataset。

## 13.1.1 Adapter 不得直接访问 Repository

```text
Adapter  →  AER Runtime / RunContext     ✅
Adapter  →  SQLite Repository            ❌
```

否则会绕过 terminal-state guard、单一错误管道与 sequence 分配。

为此 `RunContext` 增加了一组**外部事件 API**（`run.external(source=...)`）：

```python
recorder = run.external(source="adapter:aer-codex")

recorder.event(EventType.TOOL_CALL, input={"tool": "shell"})
recorder.failure(error_type="shell.NonZeroExit", message="2 failed")
recovery = recorder.recovery_started("grant edit_posts")
recorder.recovery_finished(recovery.id, success=True)
```

四个操作对应外部事件的真实形状：

```text
失败是"被描述"的，不是被抛出的 —— 不能把 Adapter 自己的调用栈当成 Agent 的 traceback
recovery 的开始与结束分两次到达 —— 可能跨越进程重启
人类反馈可以晚于终态到达 —— 它是关于 Run 的观察，不是 Agent 的变更
```

## 13.1.2 协议词汇

```text
AER_ADAPTER_PROTOCOL_VERSION = "1"     # 与包版本无关，独立命名（D-084）

AgentIdentity         provider / agent_name / agent_version / model /
                      model_version / adapter_name / adapter_version /
                      session_id / external_run_id
AdapterCapabilities   见 13.1.4
AgentAction           kind / name / tool_name / input_summary
AgentObservation      kind(TOOL_SUCCESS|TOOL_FAILURE|ENVIRONMENT|HUMAN) /
                      summary / detail
AgentExecutionEnvelope event_type / identity / protocol_version /
                      external_event_id / external_session_id / external_run_id /
                      external_timestamp / external_sequence /
                      action / observation / payload / metadata
AgentAdapter          name / protocol_version / identity / capabilities /
                      start / handle_event / finish
```

```text
不要依赖 model 名称判断 Agent 类型；model 只是描述。
厂商时间戳只进 external 元数据；AER 的 created_at 永远是接收时钟。
外部 sequence 原样保留，绝不重写 AER 的到达顺序。
AER 自己生成内部 ID；外部 ID 永不作为主键。
```

## 13.1.3 事件词汇：不新增 EventType

Adapter 事件只能映射到已有语义：

```text
MODEL_CALL / MODEL_RESULT / TOOL_CALL / TOOL_RESULT /
ERROR / RECOVERY_START / RECOVERY_RESULT / HUMAN_FEEDBACK
```

```text
TASK_START / TASK_END   由 open / close session 驱动
VERIFICATION            只能由 Verification Engine 写入；Adapter 只能把证据
                        交给真正的 verifier（D-082 / 第 39 节）
```

`decision_summary` 承载**可审计的行动理由摘要**。`chain_of_thought` /
`reasoning` / `scratchpad` 等私有推理字段一律丢弃并记录键名。

## 13.1.4 能力声明是强制的

```text
tool_events              才能写 TOOL_CALL / TOOL_RESULT
human_feedback           才能写 HUMAN_FEEDBACK
explicit_adoption_signal 才能写任何非 UNKNOWN 的 usage_signal
explicit_utility_signal  才能写 utility_label
external_verification    才能把证据交给 verifier
```

默认全部 `false`。**不能观测就只能留下 UNKNOWN**，不得推断 ADOPTED；
任务成功也**不得**让 Adapter 写 HELPFUL。

## 13.1.5 会话与幂等

```text
adapter_sessions   (provider, external_session_id) 唯一 → 当前 AER Run
                   首次 STARTED / 重连 RESUMED / 已终态默认拒绝
                   显式 reopen() → REOPENED，旧 run id 进 previous_runs

adapter_events     (provider, external_event_id) 唯一 → 幂等账本
                   先 claim（applied=false）→ apply → mark_applied
                   重复投递 = DUPLICATE，不写任何东西
```

## 13.1.6 外部输入治理

```text
按值脱敏：Bearer / key=value / URL 内联密码 / PEM / SSH / 厂商 key 前缀
按键脱敏：api_key / authorization / cookie / token / password / credentials ...
丢弃私有推理键（记录键名，不脱敏）
尺寸上限：单字符串 4096 / decision_summary 2048 / body 16384 / 深度 8
超限一律显式截断标记，禁止静默截断
```

Prompt injection 不靠匹配处理，而是**结构上不可达**：协议里没有任何字段能让
外部字符串变成 AER 指令。

## 13.1.7 参考实现与验收

```python
from aer import AER, GenericAgentAdapter

with AER("./data") as aer:
    aer.adapter_registry.register(GenericAgentAdapter)
    handle = aer.open_adapter_session("aer-generic", {"session_id": "s1", "task": "..."})
    aer.ingest_adapter_event(handle, {"type": "tool_call", "tool": "shell"})
    aer.close_adapter_session(handle, {"status": "success"})
```

`GenericAgentAdapter` 只是**参考实现**，不是生产 Adapter。它的作用是证明协议足够：
两个完全不同的假 Agent（shell 风格 / structured 风格）通过同一协议产生**逐条相同**
的 AER 语义，而 Core 一行未改。

---

# 14. Context Manager

优先使用 Python：

```python
with ...
```

实现 Hook 生命周期。

这叫：

```text
Context Manager
上下文管理器
```

它应自动记录：

```text
start_time
end_time
duration
exception
result
```

业务代码不应该手动重复编写这些逻辑。

---

# 15. Repository Pattern

Repository：

> 数据仓储模式。

业务服务不得直接依赖 SQLite SQL。

例如：

```python
class ExperienceRepository:
    def create(self, experience): ...
    def get(self, experience_id): ...
    def search(self, query): ...
    def update(self, experience): ...
```

未来迁移数据库时，只替换：

```text
SQLiteRepository
```

而不是修改整个业务层。

---

# 16. Storage Rule

SQLite 保存：

```text
结构化数据
元数据
状态
统计
索引
```

文件系统保存：

```text
Screenshot
HTML
Log
CSV
JSON
Report
Attachment
```

禁止将大型二进制内容直接大量写入 SQLite。

文件路径记录到：

```text
artifacts
```

表。

---

# 17. Core Tables

MVP 核心表：

```text
runs
events
artifacts
errors
verifications
experiences
experience_sources
retrieval_sessions
experience_usage
workflows
dataset_items
```

已落地（M1–M8 共 11 张业务表）：`runs` / `events` / `errors` / `recoveries` /
`verifications` / `experiences` / `experience_sources` / `retrieval_sessions` /
`experience_usage` / `adapter_sessions` / `adapter_events`。
`artifacts` / `workflows` / `dataset_items` 仍未建表。

不要在第一阶段随意扩充大量数据库表。

---

# 18. Verification

Verification：

> 对 Agent 执行结果进行独立验证。

优先级：

```text
Deterministic Verification
确定性验证

>

Environment Verification
环境验证

>

Human Verification
人工验证

>

LLM Judge
模型评估

>

Agent Self Evaluation
Agent 自评
```

永远不要把：

```text
Agent 认为自己完成
```

直接等价为：

```text
任务成功
```

---

# 19. Deterministic Verification

能用代码判断的事情必须用代码。

例如：

```python
assert response.status_code == 200
assert len(h1_nodes) == 1
assert schema_valid is True
```

禁止用 LLM 判断这种确定性问题。

---

# 20. WordPress Verification

WP Agent 第一阶段应支持：

```text
HTTP Status
DOM
H1
Title
Meta Description
Canonical
Schema
REST API Result
Broken Link
Basic JS Error
```

例如：

```text
H1 == 1
HTTP == 200
Schema valid
```

属于代码验证。

---

# 21. Experience

Experience：

> 从一个或多个真实 Run 中提炼出的可复用经验。

Experience 不等于 Chat History。

Experience 应至少包含：

```text
domain
title
problem
symptoms
root_cause
solution
workflow
avoid
confidence
status
statistics
```

---

# 22. Experience Distillation

Distillation：

> 经验提炼。

不是所有 Run 都需要提炼。

只有以下情况优先进入 Distiller：

```text
出现 Error
出现 Recovery
人工进行了关键修改
出现新问题
任务价值较高
重复 Retry
现有 Experience 无法解决
```

普通成功任务不要浪费 Token 提炼。

---

# 23. Experience Distiller Output

标准输出：

```json
{
  "title": "",
  "domain": "",
  "problem": "",
  "symptoms": [],
  "failed_attempts": [],
  "root_cause": "",
  "solution": "",
  "recommended_workflow": [],
  "avoid": [],
  "generalizable": true,
  "confidence": 0.0
}
```

其中：

`generalizable`

表示：

> 该经验是否具有跨任务复用价值。

---

# 24. Experience Lifecycle

状态严格使用：

```text
RAW
↓
DISTILLED
↓
VERIFIED
↓
REUSED
↓
PROVEN
↓
TRAINING_CANDIDATE
↓
TRAINING_DATA
```

失效状态：

```text
DEPRECATED
```

各状态含义：

```text
RAW        候选经验已落库，尚未完成提炼
DISTILLED  轨迹已压缩为结构化经验陈述
VERIFIED   该陈述的核心事实已有外部证据支持
REUSED     被注入到一个**不是它来源 Run** 的真实 Run 中
PROVEN     在多个不同的真实 Run 中被**明确采用**，且其中多数为 verified success，
           并且没有任何 harmful 反馈
```

顺序说明（M5 修正）：原文档写的是

```text
RAW → VERIFIED → DISTILLED
```

这在语义上不成立：验证是**对某条陈述的检验**，而陈述只有提炼之后才存在。
因此正确顺序是：

```text
RAW → DISTILLED → VERIFIED
```

改动理由与影响见 `docs/DECISIONS.md` D-029。

禁止任务一成功就直接进入：

```text
TRAINING_DATA
```

M7（Usage）落地后允许的转移：

```text
RAW         → DISTILLED
RAW         → DEPRECATED
DISTILLED   → VERIFIED
DISTILLED   → DEPRECATED
VERIFIED    → REUSED          （需要 usage 证据，由 promotion policy 决定）
VERIFIED    → DEPRECATED
REUSED      → PROVEN          （阈值见 #26.5）
REUSED      → DEPRECATED
PROVEN      → DEPRECATED
```

`TRAINING_CANDIDATE` / `TRAINING_DATA` 仍然**没有任何入边**：把经验变成训练数据需要
Dataset Builder、去重、安全审查与 Holdout 隔离，都不属于本轮（见 #26.7）。

**状态由数据决定，不由写入者决定**。`REUSED` / `PROVEN` 只能通过
`aer.promote_experience(...)`（即 promotion policy）到达，检索或打信号**都不会**
顺手改状态。理由：一个会随计数器自动变动的状态，事后无法审计。

---

# 25. Experience Source

一条 Experience 可以关联多个 Run。

例如：

```text
Experience
WordPress REST API 403 权限问题

来源：
run_001
run_017
run_083
```

重复问题优先合并来源。

不要不断制造重复 Experience。

**来源 Run 不算 Reuse**。一条经验从 run_001 提炼出来，再被注入回 run_001，不能证明
任何东西——那是它唯一被保证相关的场景（D-070）。

---

# 26. Experience Usage

> 一条 Experience 被检索之后，到底有没有真正进入 Agent 上下文、Agent 有没有采用、
> 最后任务结果如何。

## 26.1 四个区别必须进入数据模型

```text
Retrieved  ≠  Injected     检索结果没人渲染 ≠ 被使用
Injected   ≠  Adopted      进了上下文 ≠ Agent 采用了
Adopted    ≠  Helpful      Agent 可能采用了一条错误的经验
Success    ≠  Caused      任务成功 ≠ 这条经验导致了成功
```

**禁止**把「被检索」直接当作 `reuse_count + 1`；**禁止**把「被注入 + Run SUCCESS」
直接判定为「这条经验有效」。两者都是错误归因。

## 26.2 事实层：SQLite

Usage 是 **Agent 行为事实**，因此存 SQLite，不进 NeuG：

```text
retrieval_sessions   一次检索（可 0 结果）
experience_usage     一次检索 × 一条经验
```

NeuG 本轮保持**只读**语义：检索 / 注入 / 使用统计都不会修改 Experience Node，
也不会写 `NeuG.reuse_count`。Usage 的 Source of Truth 只有 SQLite。

## 26.3 记录什么

每次检索记录：

```text
session：run_id(nullable) / query_text(sanitized) / query_fingerprint / domain /
         mode / requested_limit / result_count / projection & policy version /
         duration / created_at
usage  ：rank / role / retrieval_score / retrieved_at
         injected_at / injection_position / injection_chars /
         context_fingerprint / formatter_version
         usage_signal + source / utility_label + source
```

每次检索的两条规则：

```text
query 必须 sanitized（redact + 截断），不得把原始 prompt / tool output 存进去
0 结果也必须保存——「查过但没有」和「根本没查」是两种不同的状态
```

`usage_signal`：

```text
UNKNOWN   没有可靠证据知道 Agent 是否采用（默认值，也是绝大多数行的真实状态）
ADOPTED   有显式信号证明 Agent 使用了该经验
IGNORED   明确知道经验进入上下文但未被采用
REJECTED  Agent / Human 明确判断该经验不适用于当前任务
```

`utility_label`：

```text
UNKNOWN / HELPFUL / NEUTRAL / HARMFUL
```

## 26.4 三条不可越过的边界

```text
1. 不自动推断。文本相似、工具调用类似，都不许把 UNKNOWN 改成 ADOPTED。
2. 信号必须带来源（AGENT / HUMAN / ADAPTER / EVALUATOR / SYSTEM），
   且冲突信号必须显式 override，不许静默翻转。
3. 不存原文。只存 experience id / position / char count / formatter version /
   context fingerprint；不存完整 prompt、context 或 chain-of-thought。
```

`retrieve()` 保持**纯读**（无 Usage 副作用）。需要记账时用
`retrieve_for_run()`——后台查询、调试检索、测试查询都不会污染真实 Usage。

## 26.5 生命周期与 Promotion 阈值

```text
VERIFIED → REUSED   被注入到一个非来源 Run（不要求该 Run 成功）
REUSED  → PROVEN    见下
```

`PROVEN` 默认阈值（全部可配置，见 `PromotionPolicy`）：

```text
distinct adopted runs           >= 5
adopted verified successes      >= 4
adopted verified success rate   >= 0.80
harmful feedback                == 0
```

**只有 Injected 而全部 UNKNOWN 时：可以 REUSED，但默认不得自动 PROVEN。**
因为系统甚至不知道 Agent 有没有看过这条经验。

`PROVEN` 的准确语义是：

> 在多个真实任务中被**明确采用**，并与多次 verified success **共现**。

它仍然**不是** causally proven。

## 26.6 Effectiveness 报告

```python
report = aer.experience_effectiveness(experience_id)
```

包含：`retrieval_count` / `injection_count` / `explicit_adoption_count` /
`explicit_ignore_count` / `explicit_rejection_count` / `helpful_count` /
`neutral_count` / `harmful_count` / `distinct_target_runs` /
`verified_success_runs` / `verified_failure_runs` / `run_failed_runs` /
`unverified_runs` / `running_runs` / `unattributed_usage_count` /
`observed_success_rate` / `injected_verified_success_rate` /
`adopted_verified_success_rate`。

三条计算规则：

```text
1. 计数以「不同的 run」为单位，不以 usage 行——一次 Run 里检索十次只是一次证据。
2. 分母只算已经 Injected 且 target run 最终有 Verification 的 run。
   UNVERIFIED 不算失败，也不进分母。
3. adoption 子集单独报告，不与 injected 子集混合。
```

名字必须是 `observed_success_rate`，不是 `effectiveness`：

```text
P(success | experience injected)
≠
P(success | do(experience injected))
```

观察相关性 ≠ 因果作用。真正的因果证据需要 randomized holdout / A/B，
字段（`experiment_id` / `assignment`）已预留，本轮不实现。

## 26.7 本轮不做

```text
不建 Dataset Builder，不导出 preference dataset，不训练
不做 A/B 实验框架
不做 Agent Adapter（接口留着，实现留给 M8）
Usage 不投影进 NeuG
```

---

# 27. Confidence

Confidence：

> 经验可信度。

不要由模型随意生成最终值。

M7 起实现为**确定性、可拆解**的加权和，按需计算、**不写回**：

```text
confidence = 0.30 * verification + 0.25 * reuse + 0.25 * outcome
           + 0.10 * feedback     + 0.10 * freshness
```

```text
verification  该陈述是否已被验证且 outcome 有证据支持（0/1）
reuse         被注入过多少不同的 Run，在 5 个 Run 处饱和
outcome       优先取 adopted verified success rate，其次 injected 口径
feedback      显式 utility 标签；有 harmful 直接归零；无反馈取 0.5（中性）
freshness     与排序同一条衰减曲线（半衰期 180 天）
```

两条硬性要求：

```text
同样的证据 + 同样的时钟 => 同样的 confidence（完全确定性，禁止 LLM 打分）
按需计算，不落库（D-072）
```

模型给出的 Confidence 只能作为**输入之一**，不能作为最终值。

---

# 28. Retrieval

Retrieval：

> 在新任务执行前寻找相关历史经验。

M6 起实现为：

```text
SQLite（事实源）
   ↓ 单向投影
NeuG 图索引（可重建）
   ↓
BM25 + 图过滤（APPLIES_TO / DERIVED_FROM）
   ↓
策略（GUIDANCE / DIAGNOSTIC / ALL）→ 排序 → 角色化文本
```

三条必须遵守的规则：

```text
SQLite 是唯一事实源；NeuG 只是投影，任何时刻都可以重建。
experience 与 runtime 层不得 import neug；所有引擎操作集中在
aer/knowledge/neug.py。
检索本身是只读的：retrieve() 不写 usage、不改 statistics、不改 Experience Node。
```

**记账必须显式**：需要记录 usage 时用 `retrieve_for_run()`（M7），它同样不修改
NeuG，只往 SQLite 的 `retrieval_sessions` / `experience_usage` 写事实。原因很实际：
如果所有搜索都记账，管理后台检索、调试检索、测试查询都会污染真实 Agent Usage。

**角色不能混**：`SUCCESS` / `RECOVERY`（且已验证）进 `guidance`，
`FAILURE` 与未验证观察进 `warnings`。`FAILURE` 永远不作为方案输出。

**不可达 ≠ 空**：知识索引打不开时抛 `KnowledgeIndexUnavailable`，
绝不返回空结果——"没有经验"和"知识库坏了"是两件事。

---

# 29. Semantic Retrieval

第二阶段增加：

```text
Embedding
```

::: warning 不要提前做
M6 明确只做 BM25 + 图过滤（`docs/DECISIONS.md` D-056）：在真实检索数据证明
BM25 存在明显语义召回问题之前引入向量，只会让"检索不好用"变成
"不知道是分词、BM25、embedding 还是融合的问题"。
:::

Embedding：

> 将文本转化为向量表示，用于语义相似度搜索。

经验数量小于约数万条时，不需要独立 Vector Database。

可以：

```text
SQLite
+
Python
+
Cosine Similarity
```

实现。

---

# 30. Hybrid Retrieval

最终推荐：

```text
Hybrid Retrieval
混合检索
```

综合：

```text
Keyword Similarity
关键词匹配

Semantic Similarity
语义相似度

Confidence
可信度

Success Rate
历史成功率

Version Compatibility
版本兼容性

Freshness
经验时效性
```

---

# 31. Retrieval Result Size

默认：

```text
Top 3
```

最多：

```text
Top 5
```

禁止一次向 Agent 注入大量历史经验。

避免 Context 污染。

---

# 32. Recovery

Recovery：

> Agent 执行失败后成功恢复的过程。

Recovery Experience 是高价值数据。

标准结构：

```text
Problem
↓
Failed Attempt
↓
Diagnosis
↓
Root Cause
↓
Correct Action
↓
Verification
```

应完整保留。

---

# 33. Failure

失败任务不得直接删除。

Failure 可以用于：

```text
Error Analysis
错误分析

Recovery Search
恢复检索

Evaluation Dataset
评测数据

Guardrail
安全护栏

Workflow Improvement
工作流优化
```

---

# 34. Error Fingerprint

Error Fingerprint：

> 错误指纹。

建议从：

```text
tool
error_type
status_code
key_message
environment
version
```

生成标准错误特征。

用于匹配历史 Recovery Experience。

---

# 35. Workflow

当多个 Experience 形成稳定重复流程时，可以升级成 Workflow。

Workflow：

> 标准工作流。

例如：

```text
WP Category SEO Optimization
```

可以包括：

```text
获取页面
↓
分析现有内容
↓
生成 H1
↓
生成 Intro
↓
生成 Buying Guide
↓
生成 FAQ
↓
生成 Meta
↓
生成 Schema
↓
写入 WP
↓
验证
↓
审批
↓
发布
```

---

# 36. Skill Promotion

如果某项经验长期重复且规则稳定，应考虑升级为 Skill。

例如：

```text
Experience:
频繁检测 H1
```

升级为：

```text
Skill:
wp-check-h1
```

原则：

```text
能变成代码
不要永远留在 Prompt

能变成 Skill
不要永远留在 Experience
```

---

# 37. Dataset

Dataset：

> 训练或评估模型的数据集合。

MVP 只允许生成：

```text
TRAINING_CANDIDATE
```

暂时不要自动训练模型。

---

# 38. Dataset Lineage

Lineage：

> 数据血缘。

每条 Dataset Item 必须可以追踪：

```text
Source Run
↓
Experience
↓
Verification
↓
Dataset Version
↓
Model Version
```

禁止生成无法追踪来源的数据。

---

# 39. Security

所有数据写入 Storage 前必须经过：

```text
Sanitizer
```

Sanitizer：

> 数据脱敏与清洗模块。

默认隐藏：

```text
API Key
Password
Bearer Token
Cookie
SSH Private Key
Database Password
Secret
Credential
```

---

# 40. Sensitive Data Rule

禁止 Experience Store 保存：

```text
真实密码
真实 Token
完整 Cookie
真实 SSH Key
真实数据库连接密码
用户隐私内容
```

允许保存：

```text
credential_type
auth_method
error_type
```

例如：

正确：

```text
credential_type = wordpress_service_account
```

错误：

```text
password = abc123456
```

---

# 41. Prompt Injection

来自网页、文件、API 返回的数据默认属于：

```text
Untrusted Content
非可信内容
```

不得自动当作：

```text
System Instruction
```

或：

```text
Experience Rule
```

写入长期记忆。

---

# 42. Version Awareness

Experience 应尽可能记录：

```text
WordPress Version
Plugin Version
API Version
Agent Version
Skill Version
Workflow Version
Model Version
```

版本明显不兼容时降低 Retrieval Rank。

---

# 43. Logging

Log 应记录：

```text
run_id
event_id
module
level
message
timestamp
```

不要依赖纯文本日志作为唯一数据来源。

结构化 Event 才是主数据。

---

# 44. Testing

所有核心模块必须有测试。

至少覆盖：

```text
Run lifecycle

Event ordering

SQLite persistence

Hook exception capture

Verification

Experience promotion

Experience search

Usage statistics

Sanitization
```

---

# 45. Determinism

以下逻辑应保持确定性：

```text
status transition
confidence calculation
promotion rules
verification rules
database persistence
deduplication rules
```

不要让 LLM 控制这些核心状态机。

---

# 46. LLM Responsibilities

LLM 可以负责：

```text
Experience Distillation
问题概括

Root Cause Candidate
根因候选

Workflow Abstraction
工作流抽象

Semantic Classification
语义分类
```

但最终状态变更必须由代码执行。

---

# 47. Anti-Patterns

禁止以下设计。

## 47.1 任务成功立即训练

禁止：

```text
Run Success
↓
Training
```

---

## 47.2 所有日志直接送进模型

禁止：

```text
巨大 Raw Log
↓
直接 Context
```

必须先提炼。

---

## 47.3 Agent 自评即成功

禁止：

```text
Agent:
“任务已经成功”
```

直接将 Run 标记为 SUCCESS。

---

## 47.4 把所有问题交给 LLM

例如：

```text
HTTP 200?
```

不应该调用模型判断。

---

## 47.5 提前微服务化

禁止第一阶段拆：

```text
Experience Service
Verification Service
Event Service
Dataset Service
```

全部保持单体模块化架构。

---

## 47.6 提前增加 Kafka / Redis

除非已经观察到真实性能问题。

---

## 47.7 绕过 Repository

业务模块禁止大量直接执行 SQL。

---

# 48. Development Style

代码优先考虑：

```text
Readable
可读

Explicit
显式

Typed
类型明确

Testable
可测试

Replaceable
可替换

Simple
简单
```

不要为了抽象而抽象。

---

# 49. Type Hints

Python 新代码应尽可能使用 Type Hint。

例如：

```python
def get_experience(
    experience_id: str
) -> Experience | None:
    ...
```

避免大量：

```python
dict
```

在核心领域层自由传播。

优先使用：

```text
Pydantic Model
Dataclass
Typed Object
```

---

# 50. Error Handling

错误必须区分：

```text
Business Error
业务错误

Tool Error
工具错误

Infrastructure Error
基础设施错误

Verification Error
验证错误

LLM Error
模型错误
```

不要所有异常都只记录：

```text
Exception
```

---

# 51. Retry

Retry：

> 重试。

不得无限重试。

每个 Tool 应定义：

```text
max_retry
retryable_errors
backoff
```

Backoff：

> 重试等待策略。

相同错误重复出现时，应优先搜索 Recovery Experience。

---

# 52. First Production Scenario

第一个完整验证场景：

```text
WordPress Agent
```

优先任务：

```text
页面 H1 修改
Meta 修改
分类页内容修改
REST API 操作
页面验证
常见错误恢复
```

不要第一阶段同时接入所有企业业务。

---

# 53. MVP Evaluation

必须支持对比：

```text
With Experience
```

和：

```text
Without Experience
```

至少观察：

```text
Task Success Rate
任务成功率

First Pass Success Rate
首次成功率

Retry Count
重试次数

Tool Call Count
工具调用次数

Execution Time
执行时间

Token Usage
Token 使用量

Human Intervention
人工介入率
```

---

# 54. Architecture Boundary

AER 负责：

```text
Observe
观察

Record
记录

Verify
验证

Distill
提炼

Retrieve
检索

Evaluate
评估

Promote
晋升
```

AER 不负责：

```text
所有业务规划
所有 Tool 实现
所有业务逻辑
所有 Agent Prompt
```

---

# 55. Final Architecture Rule

整个项目必须始终满足：

```text
Agent Runtime
负责完成任务

AER Runtime
负责从任务执行过程中积累经验
```

两者不要混为一个巨型 Agent。

---

# 56. Development Decision Priority

当存在多个实现方案时，按照以下顺序选择：

```text
1. 最简单且可验证

2. 最少外部依赖

3. 最容易测试

4. 最容易替换

5. 最容易迁移

6. 最容易观测

7. 最后才考虑理论上的高扩展性
```

---

# 57. Definition of Done

AER 第一版不是以：

```text
代码写完
```

作为完成标准。

而是必须真实出现：

```text
Run A
↓
遇到问题
↓
踩坑
↓
解决
↓
Verifier 验证
↓
生成 Experience A
```

然后：

```text
Run B
↓
遇到类似问题
↓
检索 Experience A
↓
避免之前的错误
↓
成功完成
↓
Verifier 验证
```

当这个闭环能够稳定重复时：

```text
AER MVP = 成立
```

---

# 58. Guiding Statement

开发任何新功能之前，先回答：

> 这个功能是否能帮助 Agent 将真实任务中的成功、失败和恢复转化为以后可以复用的可靠经验？

如果答案是否定的，它大概率不属于当前 AER MVP。
---

# 13.2 Codex Adapter（M8.1）

Codex CLI 通过**公开生命周期 Hook**接入，不 fork、不打补丁、不新建服务。

```text
Codex hook (stdin JSON)
    ↓
aer.adapter.codex.mapping       厂商 payload → AgentExecutionEnvelope（纯函数）
    ↓
aer.adapter.codex.adapter       CodexAdapter：identity / capabilities / 终态决策
    ↓
aer.adapter.codex.hook          Codex 调用的命令（python -m aer.adapter.codex.hook）
    ↓
AdapterIngestor → RunContext
```

厂商代码**只在 `aer/adapter/codex/`**；`aer/runtime/` 里没有任何 `if provider == "codex"`。

## 13.2.1 生命周期（实测决定）

```text
SessionStart      不建 Run（payload 里没有 task 字段，D-091）
UserPromptSubmit  用 prompt 的脱敏摘要建 Run
PreToolUse        → TOOL_CALL
PostToolUse       → TOOL_RESULT（明确失败时先 ERROR）
其他十一个事件     显式 IGNORED，并在 coverage 里标 MISSING（D-092）
SessionEnd        **唯一** terminal authority；Stop 不终止 Run（D-087）
```

```text
SessionEnd.reason 能识别出结果 → 用该结果
                 识别不出     → ABORTED + metadata.codex_outcome_stated=false（D-093）
```

## 13.2.2 四条不可越过的边界

```text
Codex 说"测试通过"   ≠ Verification（Adapter 无 external_verification 能力）
工具调用成功         ≠ adoption（Adapter 无 explicit_adoption_signal 能力）
hook 执行成功        ≠ injection（AER 不写 record_injection）
会话结束             ≠ 任务成功（未声明 → ABORTED）
```

## 13.2.3 幂等与 crash gap

```text
同一 hook 重复投递 → 同一 external_event_id → 一条 AER 事件
crash gap          可检测（adapter_status()["unapplied_events"]），不声称 replay（D-094）
```

## 13.2.4 覆盖度是数据，不是文案

```bash
python -m aer.adapter.codex.hook --print-coverage
python scripts/probe_codex_hooks.py --output coverage.json     # 升级 Codex 前先跑
```

`aer/adapter/codex/coverage.py` 按 `exec` / `interactive` **分别**记录每个事件是否被
观测到。`MISSING` 就是没观测到——因为"没有工具事件的 Run"与"这次没用工具"在数据里
完全一样，只有把缺口写出来才能区分。

---

# 13.3 Outcome Semantics（M8.1.1，D-100）

Run 的终态有两类来源，AER 用一个状态把它们的区别写清楚：

```text
RUNNING          未结束
SUCCESS          声明：完成了
PARTIAL_SUCCESS  声明：部分完成
FAILED           声明：失败
ABORTED          声明：放弃
INCONCLUSIVE     没有声明；Run 结束了，但没人说结果如何
```

`INCONCLUSIVE` **不是失败的近义词**。它不携带任何关于工作的断言，
所以它有两个出口（都经 AER 而不是 adapter 决定）：

```text
INCONCLUSIVE + required 验证全部通过  → verified_success = true → kind = SUCCESS / RECOVERY
INCONCLUSIVE + 没有任何 required 验证 → 没有 kind 可写，不产出经验
INCONCLUSIVE + required 验证失败      → 证明的失败 → kind = FAILURE
```

`Run.status` 永远是**声明**，验证是并列记录的另一份事实：

```python
run.finish(RunStatus.INCONCLUSIVE)   # 集成侧：会话结束了，没人说结果
aer.verify(run_id, PytestVerifier(), required=True)   # AER 侧：独立证据
aer.verified_success(run_id)         # 派生的判断，从不写回 Run
```

四件仍然不可越界的事（全部是否证式测试）：

```text
agent 自述成功          ≠ Verification
工具调用成功            ≠ Experience 被采用
hook 执行成功           ≠ Experience 已注入
INCONCLUSIVE            ≠ FAILURE（没有验证就没有 kind）
```

**Adapter 只需回答四个词**：`declared SUCCESS` / `declared FAILURE` / `declared ABORTED` /
`outcome UNKNOWN`。"UNKNOWN + 验证通过算不算成功"由 AER 决定一次，
不在每个 adapter 里各写一份。
