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
experience_usage
workflows
dataset_items
```

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

当前只允许：

```text
RAW         → DISTILLED
RAW         → DEPRECATED
DISTILLED   → VERIFIED
DISTILLED   → DEPRECATED
VERIFIED    → DEPRECATED
```

`REUSED` 及以上需要 `experience_usage` 数据（M7），**当前不存在任何到达它们的路径**。

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

---

# 26. Experience Usage

每次检索 Experience 必须记录：

```text
retrieved
injected
useful
task_success
```

从而计算：

```text
reuse_count
success_count
failure_count
success_rate
```

Experience 的真实价值由后续任务证明。

---

# 27. Confidence

Confidence：

> 经验可信度。

不要由模型随意生成最终值。

推荐根据以下因素计算：

```text
verification
reuse_count
success_rate
human_feedback
freshness
```

模型给出的 Confidence 只能作为输入之一。

---

# 28. Retrieval

Retrieval：

> 在新任务执行前寻找相关历史经验。

MVP 第一阶段：

```text
SQLite FTS5
```

FTS：

```text
Full-Text Search
全文搜索
```

即可。

---

# 29. Semantic Retrieval

第二阶段增加：

```text
Embedding
```

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