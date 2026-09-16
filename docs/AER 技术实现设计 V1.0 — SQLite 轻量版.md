# AER 技术实现设计 V1.0

## SQLite 轻量版

**项目名称：** AER — Agent Experience Runtime  
**中文名称：** 智能体经验运行时  
**设计目标：** 单机可运行、低资源占用、低运维成本、后续可平滑扩展。

---

# 1. 技术目标

AER 第一阶段不建设大型 Agent 平台，而是解决一个具体问题：

> Agent 每一次执行产生的成功经验、失败经验、恢复过程和人工修正，如何自动记录、验证，并在下一次任务中复用。

因此第一版遵循：

```text
简单优先
单机优先
SQLite 优先
文件系统优先
同步逻辑优先
少服务优先
```

避免：

```text
PostgreSQL
Redis
Kafka
MinIO
ElasticSearch
独立向量数据库
微服务
```

除非未来确实出现性能瓶颈。

---

# 2. MVP 总体架构

```text
┌─────────────────────────────────┐
│          Business Agent         │
│                                 │
│ WP / SEO / 飞书 / CLI Agent     │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│          AER Python SDK         │
│                                 │
│ start_run()                     │
│ emit_event()                    │
│ record_error()                  │
│ record_recovery()               │
│ verify()                        │
│ end_run()                       │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│          Hook Manager           │
│            钩子管理器            │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│            SQLite               │
│                                 │
│ Runs                            │
│ Events                          │
│ Errors                          │
│ Verifications                   │
│ Experiences                     │
│ Experience Usage                │
│ Workflows                       │
│ Dataset Items                   │
└──────────────┬──────────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
 File Storage      Experience Engine
 文件存储           经验处理引擎
       │                │
 Screenshot         Distill
 HTML               Retrieve
 Log                Rank
 Attachment         Promote
                        │
                        ▼
                  下一个 Agent
```

整个 MVP 可以只有：

```text
1 个 FastAPI 进程
1 个 SQLite 文件
1 个 data 目录
```

---

# 3. 推荐技术栈

```text
Python 3.12+
FastAPI
Pydantic
SQLAlchemy 2.x
SQLite
Alembic
LangGraph（可选）
```

其中：

**FastAPI**

Python Web API 框架。

负责：

- Agent 接口；
- AER 管理接口；
- Experience 查询接口；
- Dashboard 后端。

---

**Pydantic**

Python 数据校验库。

用于定义：

```text
Event
Run
Experience
Verification
```

等数据结构。

---

**SQLAlchemy**

ORM，Object Relational Mapping。

中文可理解为：

> Python 对数据库的统一操作层。

这样未来：

```text
SQLite
→ PostgreSQL
```

迁移时业务代码不用全部重写。

---

**Alembic**

数据库迁移工具。

负责：

```text
V1 数据库
↓
新增字段
↓
V2 数据库
```

而不是每次手动修改 SQLite。

---

# 4. SQLite 配置

SQLite 建议开启：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

## WAL

WAL：

**Write-Ahead Logging**

中文：

**预写式日志。**

普通 SQLite 写数据库时容易阻塞读取。

WAL 模式下：

```text
Reader
读取

Writer
写入
```

可以拥有更好的并发能力。

对于 AER 这种：

```text
大量读取
+
少量持续事件写入
```

非常适合。

---

# 5. 文件结构

建议：

```text
aer/
├── app/
│   ├── api/
│   ├── core/
│   ├── hooks/
│   ├── models/
│   ├── services/
│   ├── repositories/
│   ├── schemas/
│   ├── agents/
│   └── main.py
│
├── data/
│   ├── aer.db
│   │
│   ├── runs/
│   │   ├── run_xxx/
│   │   │   ├── screenshots/
│   │   │   ├── html/
│   │   │   ├── logs/
│   │   │   └── attachments/
│   │
│   ├── datasets/
│   └── backups/
│
├── migrations/
├── tests/
├── pyproject.toml
└── README.md
```

SQLite：

```text
data/aer.db
```

保存元数据。

大型内容不要直接全部保存进 SQLite。

---

# 6. 为什么文件和数据库分开

例如一次网页任务可能产生：

```text
3 MB HTML
5 张截图
2 MB 浏览器日志
```

如果全部塞进 SQLite：

数据库会快速膨胀。

所以 SQLite 只记录：

```json
{
  "artifact_type": "screenshot",
  "path": "runs/run_001/screenshots/final.png"
}
```

实际文件：

```text
data/runs/run_001/screenshots/final.png
```

---

# 7. 核心数据库表

MVP 第一版建议 10 张核心表。

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

---

# 8. runs 表

记录一次完整任务。

```sql
CREATE TABLE runs (
    id TEXT PRIMARY KEY,

    task_type TEXT,

    task_description TEXT NOT NULL,

    agent_name TEXT,
    agent_version TEXT,

    model_provider TEXT,
    model_name TEXT,

    status TEXT NOT NULL,

    started_at DATETIME NOT NULL,
    ended_at DATETIME,

    final_score REAL,

    metadata_json TEXT
);
```

例如：

```text
run_20260915_001
```

---

# 9. Run 状态

统一：

```text
RUNNING
SUCCESS
PARTIAL_SUCCESS
FAILED
ABORTED
```

不要允许 Agent 自己随便创造状态名。

---

# 10. events 表

记录 Agent 执行过程。

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    run_id TEXT NOT NULL,

    sequence INTEGER NOT NULL,

    event_type TEXT NOT NULL,

    input_json TEXT,
    output_json TEXT,

    created_at DATETIME NOT NULL,

    duration_ms INTEGER,

    metadata_json TEXT,

    FOREIGN KEY(run_id)
        REFERENCES runs(id)
);
```

其中：

```text
sequence
```

表示执行顺序。

例如：

```text
1 task_start
2 model_call
3 tool_call
4 tool_result
5 error
6 recovery
```

---

# 11. Event Type

统一事件类型：

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

以后可以增加，但第一版不要太复杂。

---

# 12. artifacts 表

Artifact：

**任务产物 / 附件。**

包括：

```text
Screenshot
HTML
Log
JSON
CSV
Report
```

表：

```sql
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,

    run_id TEXT NOT NULL,

    artifact_type TEXT NOT NULL,

    file_path TEXT NOT NULL,

    mime_type TEXT,

    file_size INTEGER,

    hash TEXT,

    created_at DATETIME NOT NULL,

    FOREIGN KEY(run_id)
        REFERENCES runs(id)
);
```

---

# 13. errors 表

专门记录错误。

```sql
CREATE TABLE errors (
    id TEXT PRIMARY KEY,

    run_id TEXT NOT NULL,

    event_id INTEGER,

    error_type TEXT,

    error_message TEXT,

    stack_trace TEXT,

    recoverable INTEGER,

    resolved INTEGER DEFAULT 0,

    created_at DATETIME NOT NULL
);
```

---

# 14. verifications 表

Verifier：

**验证器。**

表：

```sql
CREATE TABLE verifications (
    id TEXT PRIMARY KEY,

    run_id TEXT NOT NULL,

    verifier_type TEXT NOT NULL,

    verifier_name TEXT NOT NULL,

    passed INTEGER NOT NULL,

    score REAL,

    result_json TEXT,

    created_at DATETIME NOT NULL
);
```

---

# 15. verifier_type

可以是：

```text
DETERMINISTIC
ENVIRONMENT
LLM
HUMAN
```

含义：

### DETERMINISTIC

确定性验证。

例如：

```python
assert response.status_code == 200
```

---

### ENVIRONMENT

环境验证。

例如真实访问网页：

```text
页面是否打开
DOM 是否存在
API 是否成功
```

---

### LLM

由大模型评估。

---

### HUMAN

人工确认。

---

# 16. experiences 表

核心表。

```sql
CREATE TABLE experiences (
    id TEXT PRIMARY KEY,

    kind TEXT NOT NULL,

    domain TEXT NOT NULL,

    title TEXT NOT NULL,

    problem TEXT NOT NULL,

    symptoms_json TEXT,

    root_cause TEXT,

    solution TEXT,

    failed_attempts_json TEXT,

    workflow_json TEXT,

    avoid_json TEXT,

    status TEXT NOT NULL,

    confidence REAL NOT NULL,

    generalizable BOOLEAN NOT NULL,

    outcome_verified BOOLEAN NOT NULL,

    dedup_key TEXT NOT NULL,

    created_at DATETIME NOT NULL,

    updated_at DATETIME NOT NULL,

    metadata_json TEXT,

    UNIQUE (id)
);

CREATE INDEX ix_experiences_dedup_key ON experiences (dedup_key);
CREATE INDEX ix_experiences_kind ON experiences (kind);
CREATE INDEX ix_experiences_status ON experiences (status);
```

与原设计的差异（M5 实现为准）：

```text
+ kind               三类经验（SUCCESS / RECOVERY / FAILURE）
+ failed_attempts_json  失败尝试，Recovery / Failure 经验的核心内容
+ generalizable      是否具备跨任务复用价值
+ outcome_verified   结果本身是否有独立证据（≠ solution 已验证）
+ dedup_key          确定性去重指纹（索引，但**非唯一**）
- reuse_count / success_count / failure_count
                     属于 experience_usage 的产物，M7 才可实现（D-037）
```

`dedup_key` 刻意不唯一：它是规范化字符串，过激的规范化绝不能拒绝一条合法经验
（D-035）。`experience_sources` 与 `runs` 之间是 `ON DELETE CASCADE`，
`experiences` 删除时来源同样级联。

---

# 17. Experience Status

状态：

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

顺序为 `RAW → DISTILLED → VERIFIED`（M5 修正，原文档写作
`RAW → VERIFIED → DISTILLED`）。验证针对的是提炼后的陈述，因此必须先提炼。
详见 `docs/DECISIONS.md` D-029。

当前仅前三个状态可达；`REUSED` 及以上等待 M7 的 `experience_usage` 数据。

---

# 18. experience_sources

一个经验可能来自多个 Run。

例如某个问题出现十次。

不能创建十条几乎完全一样的 Experience。

因此：

```sql
CREATE TABLE experience_sources (
    experience_id TEXT NOT NULL,
    run_id TEXT NOT NULL,

    PRIMARY KEY (
        experience_id,
        run_id
    )
);
```

可以形成：

```text
Experience #38

来源：
Run #100
Run #139
Run #271
Run #288
```

这比单纯保存一条经验可靠。

---

# 19. experience_usage

非常关键。

记录：

> 某一次任务有没有使用某条经验，以及用了以后结果怎么样。

```sql
CREATE TABLE experience_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    experience_id TEXT NOT NULL,

    run_id TEXT NOT NULL,

    retrieved INTEGER DEFAULT 1,

    injected INTEGER DEFAULT 0,

    useful INTEGER,

    task_success INTEGER,

    created_at DATETIME NOT NULL
);
```

这样以后可以计算：

```text
Experience #38

被检索：
52 次

真正注入：
31 次

任务成功：
28 次
```

---

# 20. Experience Success Rate

计算：

```text
28 / 31
=
90.3%
```

这就比：

> “模型说这是一条好经验。”

可靠很多。

---

# 21. workflows 表

重复成功经验升级成 Workflow。

Workflow：

**工作流 / 标准执行流程。**

```sql
CREATE TABLE workflows (
    id TEXT PRIMARY KEY,

    name TEXT NOT NULL,

    domain TEXT,

    description TEXT,

    steps_json TEXT NOT NULL,

    version INTEGER DEFAULT 1,

    status TEXT,

    success_count INTEGER DEFAULT 0,

    failure_count INTEGER DEFAULT 0,

    created_at DATETIME NOT NULL,

    updated_at DATETIME NOT NULL
);
```

---

# 22. dataset_items

未来训练时才启用。

```sql
CREATE TABLE dataset_items (
    id TEXT PRIMARY KEY,

    dataset_type TEXT NOT NULL,

    experience_id TEXT,

    source_run_id TEXT,

    input_json TEXT,

    output_json TEXT,

    chosen_json TEXT,

    rejected_json TEXT,

    status TEXT,

    created_at DATETIME NOT NULL
);
```

---

# 23. Hook SDK

Agent 接入应该非常轻。

不要让业务 Agent 感觉自己正在操作复杂平台。

例如：

```python
from aer import AER

aer = AER()

run = aer.start_run(
    task="修复 WordPress 产品页 H1"
)
```

工具调用：

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
with run.recovery(
    reason="REST API 返回 403"
):
    ...
```

验证：

```python
run.verify(
    name="h1_check",
    passed=len(h1) == 1
)
```

结束：

```python
run.success()
```

---

# 24. Context Manager

Python 推荐大量使用：

```python
with ...
```

这叫：

**Context Manager，上下文管理器。**

好处是：

如果：

```python
with run.tool(...):
```

内部代码报错，

AER 可以自动捕获：

```text
开始时间
结束时间
异常
耗时
```

业务 Agent 不需要手动写大量 Trace 代码。

---

# 25. Experience Distiller

Distiller：

**经验提炼器。**

第一版可以使用强模型。

输入：

```text
Run
+
Events
+
Errors
+
Recoveries
+
Verifications
```

输出统一 JSON：

```json
{
  "title": "WordPress REST API 403 权限错误",

  "problem": "...",

  "symptoms": [],

  "failed_attempts": [],

  "root_cause": "...",

  "solution": "...",

  "recommended_workflow": [],

  "avoid": [],

  "generalizable": true,

  "confidence": 0.85
}
```

---

# 26. 不是所有 Run 都执行 Distillation

否则浪费 Token。

第一版规则：

```text
直接成功
+
非常普通
+
历史已经有类似经验
```

不提炼。

以下情况才启动：

```text
出现 Error

出现 Recovery

人工做了明显修改

任务首次出现

高价值任务

现有经验无法解决

Agent 多次 Retry
```

---

# 27. 去重机制

新的 Experience 出现后：

```text
Experience Candidate
↓
搜索已有 Experience
```

如果高度相似：

```text
合并 Source
+
更新统计
```

而不是创建新记录。

---

# 28. SQLite 全文检索

第一阶段直接使用：

```text
FTS5
```

FTS：

**Full-Text Search**

中文：

**全文搜索。**

SQLite 原生支持 FTS5。

可以建立：

```sql
CREATE VIRTUAL TABLE experience_fts
USING fts5(
    experience_id,
    title,
    problem,
    root_cause,
    solution
);
```

这样可以搜索：

```text
WordPress REST 403
```

---

# 29. 语义检索

第一版不一定需要向量数据库。

Experience 数量：

```text
< 10,000
```

完全可以：

```text
SQLite
+
Python
+
Embedding
```

实现。

Embedding：

**文本向量表示。**

例如每个经验生成：

```text
1024 维向量
```

保存：

```text
experience_embeddings
```

---

# 30. 最轻量方案

甚至不用 sqlite-vec。

SQLite 保存：

```text
experience_id
embedding
```

Python 查询时：

```python
experiences = load_embeddings()

similarity = cosine_similarity(
    query_embedding,
    experiences
)
```

对于：

```text
1000
5000
10000
```

条经验，

这个规模完全可以接受。

---

# 31. Cosine Similarity

Cosine Similarity：

**余弦相似度。**

用于判断两个文本向量在语义上有多接近。

范围通常：

```text
-1 ~ 1
```

越接近：

```text
1
```

通常表示越相似。

---

# 32. 第二阶段可以接 sqlite-vec

如果 Experience 数量明显增长，再考虑：

```text
sqlite-vec
```

让 SQLite 本身拥有向量搜索能力。

但建议：

> 不是 MVP 必需依赖。

否则部署复杂度又会上升。

---

# 33. Hybrid Retrieval

最终检索使用：

**Hybrid Retrieval**

中文：

**混合检索。**

综合：

```text
全文关键词
+
向量语义
+
Experience Confidence
+
使用成功率
+
版本兼容性
+
时间
```

评分：

```text
score =
semantic * 0.35
+ keyword * 0.20
+ confidence * 0.20
+ success_rate * 0.20
+ recency * 0.05
```

权重以后根据真实数据调整。

---

# 34. Experience Injection

Injection：

**上下文注入。**

不要把 20 条经验全部塞进模型。

建议：

```text
Top 3
```

最多：

```text
Top 5
```

例如：

```text
以下是历史任务中经过验证的相关经验。

经验 1：
问题：
...

根因：
...

推荐做法：
...

应避免：
...

历史复用成功率：
92%
```

---

# 35. Failure Recovery

如果 Agent 当前执行失败：

```text
Error
↓
生成 Error Fingerprint
↓
搜索 Failure / Recovery Experience
↓
返回相似历史故障
↓
Agent 尝试恢复
```

Fingerprint：

**错误指纹。**

即提取：

```text
错误类型
状态码
工具
环境
关键错误消息
```

形成用于匹配的标准特征。

---

# 36. Experience Promotion

Promotion：

**经验晋升。**

例如：

```text
RAW
↓
VERIFIED
```

条件：

至少存在：

```text
一个有效 Verifier
```

---

```text
VERIFIED
↓
REUSED
```

条件：

```text
被其他 Run 使用
```

---

```text
REUSED
↓
PROVEN
```

例如：

```text
reuse_count >= 5

success_rate >= 80%

至少来自 2 个不同任务
```

具体参数配置化。

---

```text
PROVEN
↓
TRAINING_CANDIDATE
```

条件可以：

```text
reuse_count >= 10
success_rate >= 90%
无安全风险
人工审核通过
```

第一版不必真的训练。

---

# 37. Confidence Score

建议不要完全由 LLM 给分。

可以直接计算：

```python
confidence = (
    verification_score * 0.30
    + reuse_score * 0.25
    + success_rate * 0.25
    + human_score * 0.10
    + freshness_score * 0.10
)
```

这是：

**可解释评分。**

比：

```text
LLM：“我认为可信度 0.94。”
```

可靠。

---

# 38. 数据脱敏

任何 Event 写入数据库前：

```text
Raw Data
↓
Sanitizer
↓
SQLite
```

Sanitizer：

**脱敏器 / 清洗器。**

默认过滤：

```text
API Key
Authorization
Bearer Token
Cookie
Password
SSH Key
Secret
数据库连接字符串
```

例如：

```text
Authorization:
Bearer sk_xxx
```

变成：

```text
Authorization:
[REDACTED]
```

REDACTED：

**已隐藏 / 已脱敏。**

---

# 39. 数据保留

Raw Event 可以设置：

```text
90 天
```

而：

```text
Experience
Workflow
Dataset
```

长期保留。

这样避免 SQLite 无限增长。

---

# 40. SQLite Backup

备份非常简单。

例如每天：

```text
aer.db
↓
aer-2026-09-15.db
```

放：

```text
data/backups/
```

或者使用 SQLite 在线备份 API。

第一阶段不用搭大型备份系统。

---

# 41. API 设计

核心接口：

```text
POST /runs

POST /runs/{id}/events

POST /runs/{id}/verify

POST /runs/{id}/complete

GET /runs/{id}
```

经验：

```text
GET /experiences

GET /experiences/{id}

POST /experiences/search

POST /experiences/{id}/approve

POST /experiences/{id}/deprecate
```

---

# 42. Agent Runtime 接口

Agent 执行前：

```text
POST /experiences/search
```

请求：

```json
{
  "task": "WordPress REST API 返回 403",
  "domain": "wordpress",
  "environment": {
    "wordpress_version": "..."
  }
}
```

返回：

```json
{
  "experiences": [
    {
      "problem": "...",
      "root_cause": "...",
      "solution": "...",
      "confidence": 0.92
    }
  ]
}
```

---

# 43. Dashboard 第一版

不要做复杂后台。

只有四个页面：

```text
Runs
Experiences
Failures
Metrics
```

---

# 44. Runs 页面

展示：

```text
Run ID

Task

Agent

Status

Duration

Errors

Experience Used

Verification

Created At
```

点击以后查看 Timeline。

Timeline：

**时间线。**

---

# 45. Experience 页面

展示：

```text
Title

Domain

Status

Confidence

Reuse Count

Success Rate

Source Runs

Updated At
```

---

# 46. Failure 页面

用于回答：

> Agent 最近最经常在哪里失败？

按照：

```text
error_type
tool
agent
domain
```

统计。

---

# 47. Metrics

第一版只看：

```text
任务数

成功率

首次成功率

错误率

恢复率

经验命中率

经验使用次数

使用经验后的成功率

平均执行时间

平均模型调用数
```

---

# 48. SQLite 适用边界

SQLite 非常适合：

```text
单服务器
单团队
个人项目
几万～几十万 Experience/Event
中低并发
MVP
```

AER 第一阶段完全没有必要 PostgreSQL。

---

# 49. 什么时候迁移 PostgreSQL

不是按照“项目做大了”这种模糊标准。

出现以下情况再迁：

```text
多台 AER Server 同时写数据库

持续高并发写

SQLite 锁等待明显影响执行

需要复杂权限隔离

需要横向扩容

Event 达千万级

需要大量实时统计
```

否则继续 SQLite。

---

# 50. 最轻部署形态

最终可以做到：

```bash
pip install aer-runtime

aer init

aer serve
```

生成：

```text
.aer/
├── aer.db
├── artifacts/
├── datasets/
└── config.yaml
```

启动：

```text
localhost:8765
```

Agent 连接：

```python
from aer import AER

aer = AER("./.aer")
```

甚至不需要启动独立 Server。

---

# 51. Embedded Mode

建议支持：

**Embedded Mode**

中文：

**嵌入式模式。**

即：

```text
Agent
+
AER
+
SQLite
```

运行在同一个 Python 进程。

这是最轻模式。

---

# 52. Server Mode

以后再支持：

```text
Agent A
Agent B
Agent C
     ↓
AER Server
     ↓
SQLite / PostgreSQL
```

这叫：

**Server Mode**

服务器模式。

---

# 53. 两种模式统一 SDK

业务代码：

```python
aer.start_run(...)
```

不应该关心：

```text
Embedded
还是
Server
```

底层 Repository 层负责处理。

---

# 54. Repository Pattern

Repository：

**数据仓储模式。**

例如：

```python
class ExperienceRepository:
    def create(...): ...
    def search(...): ...
    def update(...): ...
```

业务逻辑不要直接写：

```sql
SELECT ...
```

这样未来：

```text
SQLiteRepository
↓
PostgresRepository
```

直接替换。

---

# 55. 核心模块代码结构

```text
aer/
├── runtime/
│   ├── run.py
│   ├── event.py
│   └── hook.py
│
├── experience/
│   ├── distiller.py
│   ├── retriever.py
│   ├── ranker.py
│   ├── promoter.py
│   └── confidence.py
│
├── verification/
│   ├── base.py
│   ├── deterministic.py
│   ├── llm.py
│   └── human.py
│
├── storage/
│   ├── sqlite.py
│   ├── files.py
│   └── repository.py
│
├── dataset/
│   ├── builder.py
│   └── exporter.py
│
└── sdk/
    └── client.py
```

---

# 56. 第一版真正的闭环

第一版只完成：

```text
Agent 执行
↓
Hook 自动记录
↓
SQLite 保存
↓
Verifier 验证
↓
发现有价值 Run
↓
Distiller 提炼 Experience
↓
Experience 搜索
↓
下一次任务注入 Experience
↓
记录这条 Experience 有没有帮助
```

到这里：

**AER MVP 就已经成立。**

没有必要先训练任何模型。

---

# 57. 第一阶段成功标准

不是：

> “系统能不能运行？”

而是：

经过 100～300 个真实任务以后：

```text
With Experience
```

的任务表现是否优于：

```text
Without Experience
```

重点观察：

```text
成功率

重复错误率

平均 Tool Call

平均重试次数

平均 Token

人工修改率

任务完成时间
```

如果没有改善：

需要改 Experience 机制。

如果改善明显：

再继续 Dataset / Training。

---

# 58. 最终技术原则

第一版坚持：

```text
SQLite > PostgreSQL

Python Function > 微服务

Filesystem > MinIO

同步调用 > 消息队列

FTS5 > ElasticSearch

Python Vector Search > 独立向量数据库

单机 > 分布式

Experience Retrieval > Fine-tuning
```

只有真实瓶颈出现以后才升级基础设施。

AER 现阶段最重要的不是“平台规模”，而是证明：

> **经过验证的 Agent 经验能否真正提高下一次 Agent 的任务成功率。**