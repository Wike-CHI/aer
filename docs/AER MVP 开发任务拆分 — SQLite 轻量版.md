# AER MVP 开发任务拆分

## 目标

第一阶段只实现：

```text
Hook
+
Trace
+
SQLite
+
Verifier
+
Experience
+
Retrieval
```

明确不做：

```text
模型训练
DPO
强化学习
分布式
复杂权限
多租户
Kafka
Redis
独立向量数据库
```

---

# 阶段 1：Runtime 核心

实现：

```text
Run
Event
Hook
Error
Recovery
Verification
```

核心接口：

```python
run = aer.start_run(...)

run.emit(...)

run.error(...)

run.recovery(...)

run.verify(...)

run.complete(...)
```

验收：

能够完整记录一次 Agent 执行轨迹。

---

# 阶段 2：SQLite Storage

建立：

```text
runs
events
errors
verifications
artifacts
```

实现：

```text
Repository
Migration
Backup
WAL
```

验收：

关闭程序重新启动以后，所有 Run 都可以恢复查看。

---

# 阶段 3：Timeline

能够读取：

```text
Run
↓
所有 Event
↓
按时间排序
```

形成：

```text
Task Start
Model
Tool
Error
Recovery
Verify
Complete
```

完整 Timeline。

---

# 阶段 4：Verifier

先做 WordPress 场景。

例如：

```text
HTTP 200

H1 == 1

Title 存在

Meta 存在

Schema JSON 可解析

指定 DOM 存在
```

验收：

Agent 说“完成”不代表完成。

只有 Verifier 通过才算成功。

---

# 阶段 5：Experience Distiller

输入：

```text
Run
Events
Errors
Recovery
Verification
```

输出：

```text
Problem
Symptoms
Root Cause
Failed Attempts
Solution
Workflow
Avoid
Confidence
```

生成：

```text
Experience Candidate
```

---

# 阶段 6：Experience Store

新增：

```text
experiences
experience_sources
```

支持：

```text
创建
查看
搜索
审核
废弃
合并
```

---

# 阶段 7：Retrieval

第一版：

```text
FTS5
+
简单关键词
```

第二版：

```text
Embedding
+
Cosine Similarity
```

最终：

```text
Hybrid Retrieval
```

---

# 阶段 8：Experience Injection

Agent 开始任务：

```text
Task
↓
Search Experience
↓
Top 3
↓
Prompt Context
```

记录：

```text
哪些经验被检索
哪些经验真正被 Agent 使用
```

---

# 阶段 9：Experience Usage

新增：

```text
experience_usage
```

记录：

```text
Retrieved

Injected

Useful

Task Success
```

开始计算：

```text
Experience Success Rate
```

---

# 阶段 10：Experience Promotion

自动处理：

```text
VERIFIED
↓
REUSED
↓
PROVEN
```

不要做 Training。

只做到：

```text
TRAINING_CANDIDATE
```

即可。

---

# 阶段 11：后台

React 第一版只有：

```text
/runs

/runs/:id

/experiences

/experiences/:id

/failures

/metrics
```

不要先做复杂 UI。

---

# 阶段 12：真实测试

选择：

```text
WP 页面修改
```

作为第一个真实任务。

连续运行：

```text
100+
```

任务。

比较：

```text
无 Experience

VS

有 Experience
```

主要指标：

```text
成功率

Retry 次数

重复错误率

Tool Call

Token

时间

人工修改
```

---

# 第一版本 Repo 结构

```text
aer-runtime/
│
├── backend/
│   ├── aer/
│   │   ├── api/
│   │   ├── runtime/
│   │   ├── hooks/
│   │   ├── storage/
│   │   ├── verification/
│   │   ├── experience/
│   │   ├── retrieval/
│   │   ├── dataset/
│   │   └── main.py
│   │
│   ├── migrations/
│   └── tests/
│
├── frontend/
│   └── React
│
├── data/
│   └── .gitkeep
│
├── examples/
│   └── wordpress-agent/
│
├── docs/
│   ├── architecture.md
│   ├── sdk.md
│   └── experience.md
│
└── README.md
```

---

# MVP 优先级

## P0

必须完成：

```text
Run

Hook

Event

SQLite

Error

Recovery

Verifier

Experience

Retrieval
```

---

## P1

重要但可以稍后：

```text
Dashboard

Embedding

Confidence

Experience Usage

Workflow
```

---

## P2

以后：

```text
Dataset Builder

Local Model

Fine-tuning

DPO

RL

Multi-Agent Experience

PostgreSQL
```

---

# MVP 完成定义

当以下闭环第一次真正跑通：

```text
第一次：

WP Agent 遇到问题 A
↓
踩坑
↓
最终解决
↓
Verifier 通过
↓
AER 自动生成 Experience A


第二次：

另一个 WP 任务遇到类似问题 A
↓
AER 检索 Experience A
↓
Agent 使用经验
↓
避免原来的错误
↓
直接解决
↓
Verifier 通过
```

就可以认为：

# AER MVP 成立。

之后才值得继续扩大架构。