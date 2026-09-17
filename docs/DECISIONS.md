# AER Decision Log

> 记录所有重要架构决策，保证可追溯。
> 格式：背景 → 问题 → 候选方案 → 最终方案 → 理由 → 是否需要重新评估。

---

## D-001 Event Sequence 的分配方式

**时间**：2026-09-15
**里程碑**：M1 / M2

### 背景

`events.sequence` 是 Run 内事件的唯一排序依据（TASKS.md Task 3.3）。
`created_at` 分辨率不足，同一微秒内的两个事件无法区分。

### 问题

`sequence` 由谁分配？如何保证单进程下不会重复或跳号？

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. Runtime 内存计数器 | `RunContext` 持有自增整数 | 进程重启后恢复执行会从 1 重新开始，与库中已有事件冲突 |
| B. 调用方显式传入 | 由业务代码负责编号 | 把核心不变量交给调用方，必然出错 |
| C. Storage 层在同一事务内 `max(sequence)+1` | Repository 负责分配 | 需要处理读-写之间的竞态 |

### 最终方案

**方案 C**，并叠加三层保护：

1. 进程内 `threading.Lock` 串行化「读 max → 插入」；
2. 分配与插入在**同一个数据库事务**内完成；
3. `UNIQUE(run_id, sequence)` 数据库约束作为最后兜底。

### 理由

- 单一事实来源在数据库，重启后自动延续编号（已验证：`test_sequence_continues_after_a_restart`）。
- 即使锁失效或出现跨进程写入，唯一约束会让冲突显式失败，而**不会**产生静默错序的 Trace。
- 分布式并发不在本轮范围（agent.md #5「单机优先」），无需引入更重的机制。

### 重新评估触发条件

出现多进程/多节点同时写同一 Run，或 SQLite 锁等待成为瓶颈时，改为
数据库序列或迁移到 PostgreSQL 的 `SEQUENCE`。

---

## D-002 JSON 序列化放在 Storage 层

**时间**：2026-09-15
**里程碑**：M2

### 背景

`runs.metadata_json` / `events.input_json` / `output_json` / `metadata_json`
均为 TEXT 列，需要 JSON 编解码。

### 问题

编解码逻辑放在领域模型、Repository 还是独立模块？

### 候选方案

| 方案 | 说明 |
| --- | --- |
| A. 领域模型内 `field_serializer` | Pydantic 直接产出字符串 |
| B. 每个 Repository 各自实现 | 就地 `json.dumps` |
| C. 独立模块 `aer/storage/converters.py` | 存储层统一编解码 |

### 最终方案

**方案 C**。

### 理由

- 领域模型必须存储无关（`Run.metadata` 就是 `dict`，不是字符串）。
- 方案 B 会造成 `json.dumps` 参数在多处复制（违反 DRY）。
- 方案 C 同时承载枚举编解码（`decode_enum`）与损坏 JSON 的显式报错，
  是「磁盘表示」这一职责的唯一归属地。
- 未来换 PostgreSQL（`JSONB`）时只改这一层，领域层零改动。

### 重新评估触发条件

几乎不需要。若换成非 JSON 存储格式（如 MessagePack），仍只改这一层。

---

## D-003 Embedded Mode 不依赖 FastAPI / Redis

**时间**：2026-09-15
**里程碑**：M1

### 背景

设计文档 §50-53 描述了 `aer serve` 的 Server Mode；TASKS.md Milestone 12 才实现 FastAPI。

### 问题

本轮 `AER("./data")` 要不要顺手起一个服务或引入连接层？

### 决策

**不起服务**。`AER` 是纯 Python 对象，构造时直接打开 SQLite 文件。

### 理由

- agent.md #5：单机优先、嵌入式优先、低依赖优先。
- agent.md #47.5 / #47.6：禁止提前微服务化、禁止提前引入 Redis/Kafka。
- 本轮没有多进程消费者，Server Mode 解决的是不存在的问题。
- 保持 `AER` 与 `RunContext` 的 API 形态在未来 Server Mode 下**不变**，
  届时只需替换 `AER` 内部的 Repository 实现（D-004）。

### 重新评估触发条件

出现「多个 Agent 进程需要共享同一 AER 实例」的真实需求时（Milestone 12）。

---

## D-004 依赖方向：storage → runtime.models

**时间**：2026-09-15
**里程碑**：M2

### 背景

Repository 需要返回 `Run` / `Event` 领域对象。

### 问题

`aer.storage` 依赖 `aer.runtime.models` 是否属于分层倒置？

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. storage 定义自己的行对象，上层做转换 | 严格单向 storage 不依赖 runtime | 引入一整套重复的数据结构 |
| B. storage 直接依赖 runtime.models | Repository 用领域语言说话 | storage → runtime 的编译期依赖 |

### 最终方案

**方案 B**。

### 理由

- Repository 的职责就是「把领域对象持久化 / 读回」，它必须认识领域类型。
- 方案 A 会创造出 `RunRow ↔ RunDto ↔ Run` 三套结构，违反 agent.md #9
  「不要创造大量语义重复的数据结构」。
- 反向依赖（runtime → repository）是通过 `AER` 注入的，双方都不 import 对方的具体实现，
  因此替换后端不需要触碰 runtime。

### 边界约束

`aer.runtime.run` / `aer.runtime.runtime` **不得** import `sqlalchemy`、
`aer.storage.models` 或持有 `Session`。此约束由验收脚本静态检查。

### 重新评估触发条件

若引入多个存储后端并需要在核心域内做多态分发时，再考虑抽出 Protocol 接口。

---

## D-005 时间统一为 timezone-aware UTC

**时间**：2026-09-15
**里程碑**：M1 / M2

### 背景

SQLite 没有时区概念，`DATETIME` 列只能存字符串。

### 问题

如何保证「Python 层永远是 aware datetime」，同时列类型仍是 `DATETIME`？

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. 直接 `DateTime(timezone=True)` | SQLAlchemy 内置 | SQLite 方言读回的是 **naive** datetime，静默破坏领域不变量 |
| B. 存 ISO-8601 TEXT | 显式、可读、可排序 | 列类型不再是 `DATETIME`，与已定 Schema 不符 |
| C. 自定义 `TypeDecorator` | 写入转 UTC 去 tzinfo，读出补回 UTC | 需要约 20 行代码 |

### 最终方案

**方案 C**（`aer/storage/models.py::UTCDateTime`），
并在领域层用 `pydantic.AwareDatetime` 拒绝 naive datetime。

### 理由

- 方案 A 的静默 naive 化是最危险的：`ended_at - started_at` 会在跨时区时算出错误时长。
- 方案 C 同时满足「列是 DATETIME」与「Python 永远 aware」两个约束。
- 写入前若收到 naive datetime 直接抛 `ValueError`，不猜测、不假设本地时区。

### 重新评估触发条件

迁移到 PostgreSQL 时改为 `TIMESTAMP WITH TIME ZONE`，`TypeDecorator` 可直接删除。

---

## D-006 `Event.sequence` 在领域模型中可为 None

**时间**：2026-09-15
**里程碑**：M1

### 背景

`EventRepository.create()` 需要为尚未持久化的事件分配 `sequence`。

### 问题

`Event.sequence` 是必填还是可空？Repository 接收 kwargs 还是领域对象？

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. 必填整数 + Repository 收 kwargs | 领域不变量最强 | 与 `RunRepository.create(run)` 签名风格不一致 |
| B. 可空 + Repository 收领域对象 | 与 Run 对称；调用方写法统一 | 「None 只代表未持久化」需要文档说明 |
| C. 引入 `EventCreate` DTO | 职责最清晰 | 为一个调用点增加一层类型 |

### 最终方案

**方案 B**，并在 docstring 中明确：`None` 仅表示尚未持久化，
一旦写入数据库必然非空且 `>= 1`（`Field(ge=1)` 双重约束）。

### 理由

- 方案 C 属于「为抽象而抽象」（agent.md #48）：目前只有一个调用点。
- 方案 A 迫使调用方先调 `get_next_sequence()` 再 create，产生两次往返和竞态窗口。
- `Event.id` 同样是服务端生成、可空，两者语义完全平行。

### 重新评估触发条件

出现第二个写入路径（例如批量导入 / HTTP API）时，再把 `EventCreate` 引入 `aer/schemas/`。

---

## D-007 `aer/schemas/` 本轮保持空实现

**时间**：2026-09-15
**里程碑**：M1

### 背景

目标目录结构中包含 `aer/schemas/`。

### 决策

只建立包与文档字符串，**不定义任何 Schema**。

### 理由

Schema 的职责是「跨进程/跨网络边界的传输契约」。Embedded Mode 直接传递领域模型，
此时引入 DTO 是没有调用方、无法验证的一层（agent.md #56 优先级 1：
最简单且可验证）。

### 重新评估触发条件

Milestone 12 启动 FastAPI 时，第一批请求/响应模型写入该包。

---

## D-008 枚举 vs 字符串：以「词汇表是否已冻结」为判据

**时间**：2026-09-15
**里程碑**：M1

### 背景

需要为 `VerificationRecord.verifier_type` 与 `ErrorRecord.error_type` 定型。

### 决策

| 字段 | 类型 | 理由 |
| --- | --- | --- |
| `RunStatus` / `EventType` / `VerifierType` | `StrEnum` | agent.md #10/#11 与设计文档 §15 已给出封闭词汇表 |
| `ErrorRecord.error_type` | `str` | agent.md #34 要求它来自实际错误特征（`status_code`、`key_message`、`tool`…），是**观测数据**而非封闭集合 |

### 理由

在词汇表尚未由真实错误样本确定之前就冻结枚举，会迫使后续里程碑
「改枚举 → 改数据库 → 写迁移」，成本高于收益。等 M4 真正记录错误后再收敛。

### 重新评估触发条件

Milestone 4（Verification）开始真实记录错误时，从实际数据中归纳出封闭集合。

---

## D-009 本轮不引入 Alembic

**时间**：2026-09-15
**里程碑**：M2

### 背景

TASKS.md Task 2.3 要求配置 Alembic；本轮任务描述为「Milestone 2 基础部分」。

### 决策

本轮使用 `Base.metadata.create_all()`（幂等），**不引入 Alembic**。

### 理由

- 本轮只有 `runs` / `events` 两张表，且项目**尚无任何生产数据**，
  不存在需要迁移的历史版本——Alembic 现在只能生成一个「初始 head」。
- 任务描述明确收窄了范围，并要求只保证 `Run + Event` 稳定。
- Alembic 的价值在第二次改 Schema 时体现，而那次改动会同时引入
  剩余 8 张表，一次性配置更合理。

### 重新评估触发条件

**下一次修改 Schema 之前必须完成 Alembic 接入**，否则将违反
「数据库结构修改必须通过 Migration」。这是本轮遗留的技术债。

---

## D-010 会话级错误翻译取代嵌套 `with storage_errors(...)`

**时间**：2026-09-15
**里程碑**：M2

### 背景

要求在 Repository 中把 `SQLAlchemyError` 翻译为 `StorageError` 且保留原始原因。

### 问题

最初的写法是每个方法双重嵌套：

```python
with storage_errors(f"create run {run.id}"):
    with self._database.session() as session:
        ...
```

### 候选方案

| 方案 | 说明 |
| --- | --- |
| A. 保持双重嵌套 | 语义清晰，但 8 个方法重复 8 次 |
| B. `with a(), b():` 合并 | 去掉一层缩进，仍需在每个方法写 `storage_errors` |
| C. 让 `Database.session(action)` 自带翻译 | 每个方法只写一次 `with` |

### 最终方案

**方案 C**。`Database.session(action=...)` 负责「提交 / 回滚 / 关闭 / 错误翻译」，
Repository 只需提供一句人类可读的 `action` 描述。

### 理由

- 一个方法里只出现一个上下文管理器，缩进更浅、更容易读。
- `action` 是强制性的语境信息，错误消息质量反而**高于**原方案：
  `"create run <id> failed: ..."` 而非泛化的数据库错误。
- 「事务生命周期」与「错误翻译」本就是同一件事的两面
  （只有当事务边界出错时才需要翻译），合并不违反单一职责。
- 顺带消除了 ruff `SIM117` 的 8 处告警，而不是用 `noqa` 压制。

### 副作用

`storage_errors` 仍保留，供引擎级操作（`create_schema` / `dispose` /
`table_names` / `read_pragmas`）使用，那里没有会话可依附。

### 重新评估触发条件

若未来需要「多 Repository 共享一个事务」，应改为显式传入 Session 的
Unit-of-Work 模式；届时错误翻译需要重新安排。

---

## D-011 `aer/errors.py` 重命名为 `aer/exceptions.py`

**时间**：2026-09-16
**里程碑**：M3

### 背景

M3 要把 `ErrorRepository` 挂到门面上，最自然的写法是 `aer.errors`。

### 问题

`aer.errors`（异常模块）与 `AER.errors`（仓储属性）同名但语义完全不同，
是一个永久性的阅读陷阱；`from aer import errors` 与 `instance.errors` 混淆概率很高。

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. 保留 `aer.errors`，门面用 `aer.error_repository` | 不改动现有代码 | 最高频的入口起了一个最啰嗦的名字 |
| B. 保留 `aer.errors`，门面用 `aer.errors` | 零改动 | 同名不同义，长期维护成本 |
| C. 异常模块改名 `aer/exceptions.py` | 两者都自然 | 一次性改动 5 个文件 |

### 最终方案

**方案 C**。

### 理由

- `AER.errors` / `AER.recoveries` 是调用者每天要敲的路径，应该最短最自然。
- 项目尚无外部使用者，改名成本最低。
- 门面属性名比模块名更常出现在业务代码里，优先照顾前者。

### 重新评估触发条件

不需要。

---

## D-012 Alembic 采纳策略：幂等 baseline

**时间**：2026-09-16
**里程碑**：M3

### 背景

M1/M2 用 `Base.metadata.create_all()` 建库，已经存在真实的 `runs` / `events` 数据。
现在要接入 Alembic，且**禁止**通过删库重建来规避迁移。

### 问题

在已有库上执行 `alembic upgrade head`，baseline 会因 `runs` 已存在而失败。

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. 人工执行 `alembic stamp 0001` | 标准做法 | 依赖人工步骤，漏做就报错 |
| B. `env.py` 检测到旧库时自动 stamp | 自动 | 隐式魔法，行为难以预测 |
| C. baseline 内部对每张表做存在性判断 | 幂等 | 迁移文件里出现条件逻辑 |

### 最终方案

**方案 C**，并在 baseline 的模块 docstring 里写明原因。

### 理由

- 同一份 revision 同时适用于空库与旧库，**不需要任何人工步骤**，也不需要猜。
- 条件逻辑只出现在 baseline 一处，后续增量迁移保持纯声明式。
- 已用测试固化：`TestAdoptionOfAPreAlembicDatabase` 覆盖「旧库 + 存量数据 + 升级后数据完好」。
- 反例验证：`TestSchemaParity` 证明迁移产出的 DDL 与 `Base.metadata` 完全一致，
  所以「不重建库」不会导致两份 schema 悄悄分叉。

### 重新评估触发条件

若将来需要把已分叉的历史库（结构不等于任何 revision）纳入管理，应改为显式
`stamp` + 人工核对，而不是继续加条件分支。

---

## D-013 迁移文件不得 import 应用代码

**时间**：2026-09-16
**里程碑**：M3

### 背景

autogenerate 把 `UTCDateTime` 渲染成 `aer.storage.models.UTCDateTime()`，且**不带 import**，
生成的 revision 直接 `NameError`。

### 问题

revision 文件应该引用应用类型，还是只用标准 SQLAlchemy 类型？

### 最终方案

在 `migrations/env.py` 提供 `render_item` 回调，把自定义类型渲染为其 DDL 等价物
（`UTCDateTime` → `sa.DateTime()`）。

### 理由

- revision 是**历史快照**，必须在该类型被重命名、移动或删除之后仍然可执行。
  引用应用代码会让历史迁移随代码一起腐烂。
- `UTCDateTime.impl` 就是 `DateTime`，两者产出的 DDL 逐字节相同，所以降级渲染无损。
- 用 `TestSchemaParity` 的 DDL 对比把「无损」这一论断变成可验证事实，而不是假设。

### 重新评估触发条件

新增自定义列类型时，需在 `render_item` 里补充映射，否则 autogenerate 会再次产出不可执行的 revision。

---

## D-014 Schema 供给的唯一入口

**时间**：2026-09-16
**里程碑**：M3

### 背景

M2 里 `Database.__init__` 通过 `create_all` 建表；M3 要求正式初始化必须走迁移。

### 问题

迁移在哪里触发？`Database` 还是 `AER`？

### 候选方案

| 方案 | 说明 | 缺点 |
| --- | --- | --- |
| A. `AER.__init__` 先迁移再开引擎 | 分层最纯 | 直接用 `Database` 的人会拿到空库 |
| B. `Database.__init__` 内部迁移 | 契约最强：「给路径就得到可用库」 | `database.py` 依赖 `migrations.py` |

### 最终方案

**方案 B**，并把共享的连接基础（URL 构造 + PRAGMA 钩子）抽到 `aer/storage/connection.py`，
使 `database.py` 与 `migrations.py` 之间是单向依赖，而不是循环。

### 理由

- 不可能出现「引擎指向一个半成品 schema」的状态，这是本里程碑最关键的不变量。
- 测试因此天然覆盖生产路径：每个 `AER(...)` 都会真的跑一遍迁移。
- 编排放 `AER` 会让 `Database` 变成一个随时可能被误用的半成品构造器。

### 性能

每次 `AER(...)` 都跑迁移是不可接受的，因此 `upgrade_to_head` 先做一次
`is_at_head` 快速判断（读 `alembic_version` + 进程内缓存的 head）。
实测：首次构造 62 ms，之后 3.9 ms。

### 重新评估触发条件

若迁移时间随表数量增长到秒级，改为显式 `aer migrate` 命令 + 启动时只做版本校验。

---

## D-015 时长一律用单调时钟

**时间**：2026-09-16
**里程碑**：M3

### 背景

M2 的 `TASK_END.duration_ms` 由 `ended_at - started_at` 计算（wall clock 相减）。

### 问题

系统时钟可能被 NTP 校正、手工调整或跨夏令时，wall clock 相减会产生负值或明显错误的耗时。

### 最终方案

- 所有 `duration_ms` 一律来自 `time.perf_counter()`（单调）；
- `created_at` / `started_at` / `ended_at` 仍是 timezone-aware wall clock，因为它们表达的是**时刻**。

### 理由

两者职责不同：时刻要能和外部时间线对齐，耗时必须在时钟跳变下仍然可信。
`RunContext` 在构造时取一次 `perf_counter()`，`TASK_END` 的 duration 也改为单调计算。

### 验证

`test_duration_comes_from_the_monotonic_clock` 直接替换 `aer.runtime.hooks.perf_counter`，
断言「恰好两次读取」且换算为毫秒结果精确 —— 证明实现真的在用该函数，而不是恰好数值接近。

### 重新评估触发条件

不需要。

---

## D-016 Payload 降级改用显式 marker

**时间**：2026-09-16
**里程碑**：M3

### 背景

M1/M2 把未知对象静默转成 `str(obj)`。

### 问题

`"value"` 与 `str(unknown_object)` 在存储后**无法区分**：既不知道发生了降级，
也无法在事后统计有多少 Trace 丢过信息。

### 最终方案

未知对象序列化为显式 marker：

```json
{"__aer_fallback__": true, "python_type": "module.Class", "repr": "..."}
```

同时收紧了「哪些类型算已知」：只保留 Enum / Pydantic / datetime / 标量 / 容器 / dataclass。
`bytes` 不再内联，改为 marker —— 避免二进制内容进入 SQLite（agent.md #16）。

### 理由

- 降级变成**可查询事实**，而不是隐形的数据损失。
- 刻意不提供「自动 `str()`」逃生口：`pathlib.Path`、`UUID`、`Decimal` 等都会变成 marker。
  这是刻意的取舍 —— 需要它们时应当**显式**加入白名单，而不是继承一个模糊的默认行为。

### 重新评估触发条件

真实 Agent 接入后，若 `Path` / `UUID` / `Decimal` 频繁出现在 payload 中，
在 `to_json_value` 里逐项加入显式分支，而不是恢复全量 `str()`。

---

## D-017 Error 管道单一化

**时间**：2026-09-16
**里程碑**：M3

### 背景

`run.error()` 与 `run.tool()` 都需要记录失败。

### 问题

是否会形成两套错误写入逻辑，导致 payload 形状、stack trace 处理、持久化逐渐分叉？

### 最终方案

`RunContext._record_error()` 是**唯一**实现：

```text
记录 ERROR Event → 创建 ErrorRecord（含 event_id 回链）→ 持久化
```

`run.error()` 直接调用它；`_RunHook._record_failure()` 也调用它。
error 同时**结构化落库**到 `errors` 表，而不只是塞进 event payload —— 因为
Failure Analysis / Recovery Retrieval / Distillation 都需要可查询的字段。

### 理由

- 单一实现意味着「错误长什么样」只有一个定义点。
- 先写 event、再写 record，保证 ErrorRecord 永远有它的 event（可回链、可重放）。
- `error_type` 用**全限定名**（`builtins.PermissionError`）而不是裸类名：
  跨模块同名异常真实存在，全限定名才能作为分组键。

### 重新评估触发条件

出现第二种错误来源（如批量导入）时，复用 `_record_error` 而不是另写一条路径。

---

## D-018 Recovery 与 Error 的显式关联

**时间**：2026-09-16
**里程碑**：M3

### 背景

`errors.resolved` 需要被置位，但「什么时候算解决了」有多种可能定义。

### 问题

能否因为「这个 Run 后来成功了」就把历史错误全部标记为已解决？

### 最终方案

**禁止推断，只认显式关联**：

```text
recovery(error_id=E) 且正常退出  →  errors[E].resolved = true
其余一切情况                     →  不改动任何 resolved
```

具体规则：

1. `__enter__` 时就校验 `error_id` 存在、且属于同一个 Run，否则在**写任何事件之前**报错；
2. 恢复失败不 resolve；
3. 无 `error_id` 的恢复不 resolve 任何东西；
4. Run 后续成功不 resolve 历史错误。

### 理由

「任务成功」与「这个具体错误被解决」是两件事。允许推断会让
`resolved` 变成猜测值的集合，而它未来要作为 Distillation 的输入信号。

### 重新评估触发条件

若出现「一个恢复同时解决多个错误」的真实场景，扩展为多对多关联表，而不是放宽推断规则。

---

## D-019 Hook 退出策略：记录但不吞掉

**时间**：2026-09-16
**里程碑**：M3

### 背景

Hook 必须在异常路径上写出结束事件，同时原异常要按调用方预期向外传播。

### 最终方案

`_RunHook.__exit__` 统一处理：

```text
1. 计算单调耗时
2. 调用子类 _finish(error, duration_ms) —— 写 ERROR（如有）+ 结束事件
3. return False（永不吞掉异常）
```

若 `_finish` **自身**失败（例如数据库不可用）：

- 原本没有异常 → 让写入失败向外抛出（它才是真问题）；
- 原本有异常 → 用 `BaseException.add_note()` 把写入失败附到原异常上，**原异常照常抛出**。

### 理由

调用方捕获的是自己抛出的那个异常对象，这一点不能被 AER 的故障破坏；
但 Trace 写入失败也不能被静默吞掉（agent.md #50），所以用 note 显式附加。

### 已知边界

当存储整体不可用时，Trace 必然不完整（结束事件写不进去）。
这不是可以「重试」解决的问题：已经在 note 中如实告知，不做无意义的重试。

### 重新评估触发条件

引入本地缓冲/落盘队列（后续里程碑）后，可以改为「先本地暂存、恢复后重放」。

---

## D-020 最小脱敏的边界

**时间**：2026-09-16
**里程碑**：M3

### 背景

agent.md #39 要求「所有数据写入 Storage 前必须经过 Sanitizer」，
但本轮明确不做完整 Sanitizer Framework。

### 问题

本轮脱敏覆盖到哪里？

### 最终方案

只覆盖**不受控文本**的两个入口：

| 位置 | 处理 |
| --- | --- |
| Fallback `repr` | 脱敏 + 按 `FALLBACK_REPR_MAX_LENGTH`(4096) 截断 |
| `stack_trace` | 脱敏（不截断，保留完整调试信息） |
| `error_message` | 脱敏 |
| 调用方传入的结构化 payload | 原样存储 |

识别规则按**凭证形状**匹配：Bearer、`authorization:`、`key=value` 赋值、
PEM 私钥块、SSH key body、`sk-`/`AKIA`/`gh[pousr]_` 前缀、`scheme://user:pass@host`。

### 理由

- 未受控文本（异常 message/repr 常内嵌 HTTP 头与 URL）是真实泄漏面；
  对结构化 payload 做全量替换会破坏合法 Trace 数据。
- 刻意**不**加「任何 ≥32 位字母数字串都脱敏」这类规则：它会误伤 Artifact 的
  SHA-256，而那正是我们要保留的完整性证据。
- 赋值类规则带负向先行断言 `(?![\w\[(])`，避免把 stack trace 源码行里的
  `password = os.environ["X"]`（并非真实凭证）误判为泄漏。

### 重新评估触发条件

Sanitizer 里程碑（agent.md 第 8 阶段）需要把覆盖范围扩展到全部 payload，
并引入可配置规则、保留期与审计。

---

## D-021 Verification 与 RunStatus 分离

**时间**：2026-09-16
**里程碑**：M4

### 背景

TASKS.md Task 4.5 要求系统能够表达「Agent 说 SUCCESS，Verifier 说 FAILED」。
最省事的实现是给 `RunStatus` 加一个 `VERIFIED` 成员，或让 Verifier 失败时把
`Run.status` 改成 `FAILED`。

### 问题

验证结论放在哪里？

### 候选方案

| 方案 | 结果 |
| --- | --- |
| A. 给 `RunStatus` 增加 `VERIFIED` / `VERIFICATION_FAILED` | 把「执行事实」和「验证事实」压成同一个字段 |
| B. Verifier 失败时把 `Run.status` 改写为 `FAILED` | 抹掉 Agent 的历史声明 |
| C. 验证记录为独立实体，`Run.status` 只表示 Agent 声明 | 两个事实并存 |

### 最终方案

方案 C。`Run.status` 取值集合保持不变（5 个），`VerificationRecord` 独立成表，
`verified_success` 是**派生**判断而非存储状态（D-024）。

### 理由

- 两者是**不同主体对同一 Run 的判断**：`Run.status` 记录 Agent Runtime 声明了
  什么，`passed` 记录独立验证器观察到了什么。把二者塞进一个字段，必然要丢弃
  其中一个——而丢失的那个恰好是 M5 Distiller 唯一有用的信息。
- 方案 B 会让「Agent 误判自己成功」这一最重要故障模式**从数据中消失**：库里
  只剩 `FAILED`，看不出是 Agent 说自己失败，还是 Agent 说自己成功而验证否定。
- 派生而非存储：`verified_success` 必须在新 Verdict 到达后重新计算，否则它只是
  另一个会漂移的缓存。

### 重新评估触发条件

若未来出现「Run 需要对外暴露一个综合状态」的接口需求，应新增一个**只读**的
聚合视图，而不是往 `RunStatus` 里加成员。

---

## D-022 Verifier 崩溃不等于 Verification Failed

**时间**：2026-09-16
**里程碑**：M4

### 背景

Verifier 是任意代码：可能缺少输入、可能内部断言失败、可能调用外部服务超时。

### 问题

Verifier 抛异常时，应当记录 `passed=false` 吗？

### 最终方案

不。Verifier 崩溃走**错误管道**，不走验证管道：

```text
Verifier 抛异常
  → 写 ERROR 事件（经 run 的统一 Error 管道，recoverable=false）
  → 写 ErrorRecord（metadata.source="verifier"）
  → 不写 VerificationRecord（一条都没有）
  → 不写 VERIFICATION 事件
  → 原异常按原样继续抛出
```

新增异常层次：`VerificationError`（验证器坏了）/ `VerificationInputError`
（拿不到证据）。二者都是 `AERError` 子类，与「验证结果为 false」完全分离。

### 理由

- 「这次检查没做成」与「这次检查做成了，结论是失败」是关于世界的两个不同事实。
  把前者写成后者，等于**凭空制造一条否定证据**，而下游（失败分析、经验提炼、
  置信度计算）无法区分二者。
- 「Verifier 没有拿到 `actual_count`」不是「H1 数量不等于 1」的证据——缺证据
  不等于反证据。因此 `VerifierInputError` 也绝不被降级成 `passed=false`。
- 异常必须继续抛出：AER 记录失败，但不替调用方决定如何处置。

### 已知边界

若存储整体不可用，崩溃可能只留下 `add_note` 而没有行（与 D-019 同一取舍）。

### 重新评估触发条件

若引入异步/后台 Verifier，需要决定重试语义：重试仍属于「崩溃」范畴，不得转为
`passed=false`。

---

## D-023 System Observation：Verification 允许在 Terminal Run 后追加

**时间**：2026-09-16
**里程碑**：M4

### 背景

M3 建立了 Terminal Run Guard：Run 进入终态后禁止再写任何事件。
但真实部署中验证几乎总是发生在 Agent 结束之后（甚至 Agent 进程已经退出）。

### 问题

验证应当发生在 `run.success()` 之前（方案 A），还是允许之后追加（方案 B）？

### 最终方案

方案 B，并明确区分两类写入：

| 类别 | 事件 | 终态后 |
| --- | --- | --- |
| Agent Mutation | `emit` / `tool` / `error` / `recovery` | **禁止**，抛 `RunStateError` |
| System Observation | `VERIFICATION` / `HUMAN_FEEDBACK` / 引擎内部 `ERROR` | **允许** |

实现为 `RunContext._append_system_event()`，白名单常量
`_SYSTEM_OBSERVATION_EVENTS`。**没有** `emit_after_finish(any_event)` 这类接口。

同时把 Hook 守卫**提前到调用点**：`run.tool(...)` / `run.recovery(...)` 现在立即
抛 `RunStateError`，而不再等到 `with` 进入时才失败（也避免「构造了但从未进入」
这种静默情况）。

### 理由

- Guard 的语义是「Agent 不得再改动自己已结束的记录」，而不是「这条 Trace 冻结」。
  验证是**别人**对这条 Run 的观察，属于追加而不是改动。
- 白名单而非通用后门：错误用法（终态后补写 `TOOL_CALL`）仍然立即失败。
- `ERROR` 在白名单里的**唯一**理由是 Verifier 崩溃必须留在同一条 Trace 上；它只
  能经引擎内部路径到达，Agent 侧 `run.error()` 依旧被拒绝。这条边界有测试固定
  （`tests/verification/test_terminal_run_verification.py`）。
- 顺序仍然连续：追加事件与普通事件走**同一个** `EventRepository.create`，只是跳过
  终态检查，`UNIQUE(run_id, sequence)` 依旧兜底。

### 重新评估触发条件

出现第三种后置观察（例如定时重跑的外部审计）时，应扩展白名单常量而不是放宽守卫。

---

## D-024 verified_success 的派生计算与 `required` 列

**时间**：2026-09-16
**里程碑**：M4

### 背景

需要一个 Run 级汇总，且不能把「没有验证」误解为「全部通过」。

### 最终方案

```text
all_passed          := total > 0 and failed == 0              # 含可选验证
all_required_passed := required_total > 0 and required_failed == 0

verified_success    := Run.status is SUCCESS
                       and required_total > 0
                       and required_failed == 0
```

三处补充：

1. `VerificationSummary.pass_rate` 在 `total == 0` 时是 `None`，不是 `0.0` 也不是
   `1.0` ——只读数字的调用方不会把未验证的 Run 看成干净的。
2. `verifications.required` 是**真实列**（`NOT NULL`），不是从验证器名字推断的。
3. `verified_success` 是纯函数 + 每次重新计算；**不写回 Run**。

### 理由

- 每个子句各挡掉一种假象：非 SUCCESS 状态挡掉「环境没问题≠Agent 完成了」；
  `required_total > 0` 挡掉「因为没有验证所以全过」；`required_failed == 0` 才是
  真正的确认。
- 没有 `required` 概念时，一个可选的 LLM 质量分（0.4 分）就能否决一个所有硬性
  检查都通过的任务。把 `required` 写进列，还保证「这次成功依赖哪些检查」在验证器
  默认值变化后仍然可从数据回答。
- 不写回 Run：一旦缓存，新增 Verdict 就必须记得刷新它；忘记刷新就会得到一个
  会被信任的错误布尔值。

### 重新评估触发条件

当出现「部分成功」的正式定义（例如 `PARTIAL_SUCCESS` 需要按验证比例判定）时，
应在此处扩展，而不是新增状态。

---

## D-025 确定性验证优先于 LLM 判断

**时间**：2026-09-16
**里程碑**：M4

### 背景

同一个需求（“H1 数量是否为 1”）既可以用代码判断，也可以让模型判断。

### 最终方案

`VerifierType` 的**声明顺序即信任顺序**（D-027），并且各类型的 `required` 默认值
不同：

| 类型 | `required` 默认 | 说明 |
| --- | --- | --- |
| `DETERMINISTIC` | `True` | 相同输入永远相同结论 |
| `ENVIRONMENT` | `True` | 检查真实外部环境 |
| `HUMAN` | `True` | 人工确认通常是权威 |
| `LLM` | **`False`** | 模型判断是可选的**评分**，不是闸门 |

### 理由

- 确定性检查可复现、可审计、零成本、可离线重跑；LLM 判断在两次运行之间可能与
  自己不一致。把可判定的事实降级给模型，等于用随机性替换事实。
- `LLM` 默认 `required=False`：让一个不稳定裁判否决一个硬性检查全过的任务，是拿
  一个模型的猜测替换另一个模型的猜测。需要模型作为验收标准时，显式传
  `required=True`，这个决定就留在数据里。
- AER **不绑定任何模型厂商**：`LLMVerifier` 接收 `judge` 可调用对象，Prompt、
  Key、超时与重试策略都属于调用方。运行时不因验证而引入 HTTP 依赖。

### 重新评估触发条件

当 M7 需要综合置信度时，可在此顺序上做加权；但**排序本身**不应改变。

---

## D-026 `score` 语义：分级质量，绝不重复 `passed`

**时间**：2026-09-16
**里程碑**：M4

### 背景

`VerificationResult` 同时有 `passed: bool` 与 `score: float | None`。

### 问题

确定性的布尔检查应该给 `score` 赋 `1.0` / `0.0` 吗？

### 最终方案

**不赋**。`HttpStatusVerifier` / `H1CountVerifier` / `JsonValidVerifier` 一律返回
`score=None`；只有真正**分级**的验证器（例如“4 个 meta 标签中了 3 个”、LLM 质量分）
才设置 `score`，并被 Pydantic 约束在 `0.0..1.0`。

规则：`passed` 是事实，`score` 是可选的分级信号，**二者互不推导**。

### 理由

- 若布尔检查恒返回 `1.0`/`0.0`，`score` 就只是 `passed` 的另一种写法，并且是一个
  可以与之漂移的独立字段；下游一旦出现 `score != passed` 的矛盾，没人知道该信谁。
- `score=None` 携带的信息是真实的：**这个验证器不做分级**。而 `pass_rate` 已经在
  汇总层提供了比例，不需要在每条记录里重复。
- 反向推导同样禁止：`score = 0.49` 绝不允许系统自行推断 `passed=false`（§22）。
  需要阈值就由具体验证器显式实现并写清语义。

### 重新评估触发条件

若 M7 的 Confidence 公式需要每个验证器都提供 `verification_score`，应在汇总层
按 `passed` 合成，而不是篡改原始 Verdict 的语义。

---

## D-027 `VerifierType` 声明顺序修正为信任序

**时间**：2026-09-16
**里程碑**：M4

### 背景

`VerifierType` 的 docstring 一直写着「按可信度排序」，但声明顺序是
`DETERMINISTIC, ENVIRONMENT, LLM, HUMAN` —— `LLM` 排在 `HUMAN` 之前，与
agent.md #18 及本轮 brief 的信任序（确定性 > 环境 > 人工 > LLM）矛盾。

### 最终方案

把成员声明顺序改为 `DETERMINISTIC, ENVIRONMENT, HUMAN, LLM`。**枚举值不变**
（仍存字符串），因此对已落库数据零影响，仅改变迭代顺序。

### 理由

- 现在没有代码依赖迭代顺序，将来会有：M7 的排序/置信度需要「从最可信开始」。
  一个与自身 docstring 矛盾的枚举是埋雷。
- 改动成本是两行，且由测试固定（`test_matches_the_documented_trust_order`）。
- Agent 自评**不**进入该枚举：`verifier_type` 里根本没有 `AGENT_SELF`，
  所以「用 Agent 自评冒充验证」在类型层面就写不出来。

### 重新评估触发条件

若出现新的验证来源（例如外部审计机构），按其证据强度插入正确位置，而不是追加到末尾。

---

## D-028 本轮明确不做的三件事

**时间**：2026-09-16
**里程碑**：M4

### 背景

Verification 极易膨胀成平台：DSL、Schema 引擎、异步队列、业务专用审计器。

### 最终方案

本轮**不实现**，并记录边界（§45-47）：

| 不做 | 边界 |
| --- | --- |
| WordPress 大而全验证器 | 只提供可复用原语（HTTP / H1 / JSON / Callable Environment），业务验证器留给业务层 |
| Verification DSL（YAML 规则/表达式树） | 第一版 Python Verifier 足够；DSL 会把「验证」变成需要被验证的东西 |
| 异步任务系统（Celery/Redis/Worker） | 同步执行；出现真实耗时瓶颈再引入 |

同样不做的还有：`Verification passed → 自动生成 Experience`（属 M5，且必须先有
Distiller 判定，否则验证通过率会直接变成经验噪声）。

### 理由

- 原语 + 统一管道已经覆盖本轮不变量（「Agent 说的成功」与「验证的成功」可同时
  表达），其余是能力膨胀而非能力缺失。
- 同步执行让「事件与记录同时写出」保持简单可证；异步会立刻引入部分写入与重放问题。

### 重新评估触发条件

- 业务层出现**三种以上**重复的验证组合 → 考虑组合原语，而不是 DSL。
- 单个 Verifier 稳定耗时超过一次交互预算 → 才考虑异步。

---

## D-029 Experience 生命周期修正为 RAW → DISTILLED → VERIFIED

**时间**：2026-09-16
**里程碑**：M5

### 背景

`agent.md #24`、`docs/TASKS.md` Task 5.2 与技术设计文档 #17 一致声明：

```text
RAW → VERIFIED → DISTILLED
```

本轮 brief §36 要求先评估这个顺序，而不是机械实现。

### 问题

Experience 是先被**验证**，还是先被**提炼**？

### 候选方案

| 方案 | 后果 |
| --- | --- |
| A. 照文档实现 `RAW → VERIFIED → DISTILLED` | VERIFIED 意味着「在还不知道要验证什么之前就已经验证过了」 |
| B. 改为 `RAW → DISTILLED → VERIFIED` | 验证的对象是提炼出的陈述，顺序可自洽 |

### 最终方案

方案 B，并**同步修改三份文档**（`agent.md #24`、`docs/TASKS.md` Task 5.2、
技术设计文档 #17），避免文档与代码再次分叉。

各状态含义：

```text
RAW        候选已落库，尚未提炼
DISTILLED  轨迹已压缩为结构化陈述
VERIFIED   该陈述的核心事实有外部证据支持
```

`distill_run()` 直接以 `DISTILLED` 落库，然后按证据决定是否
`DISTILLED → VERIFIED`：提炼在落库前**已经真的发生**，因此写 RAW 再立刻迁移
就是 brief §34 警告的「虚假生命周期」。

### 理由

- **逻辑必然，不是偏好**：验证是「对某条 claim 的检验」。claim 由提炼产生。
  先验证后提炼等于验证一个尚未成形的对象。
- 这个顺序让两个状态都**真的可区分**：`DISTILLED` = 「已提炼但证据不足」，
  `VERIFIED` = 「证据充分」。确实存在停在 `DISTILLED` 的情形（例如 agent 放弃
  任务且无人验证环境），所以它不是过渡装饰。
- `RAW` 仍然保留：它是「已持久化但尚未提炼」的入口状态，供导入、人工撰写或未来
  的异步/排队 Distiller 使用。`distill_run()` 不写它。

### 重新评估触发条件

若引入异步 Distiller，RAW 会成为「已入队待提炼」的真实状态；届时需要补上队列与
重试语义，但顺序不变。

---

## D-030 失败也要 Distill：三类 ExperienceKind

**时间**：2026-09-16
**里程碑**：M5

### 背景

最省事的策略是「只有 `verified_success == true` 的 Run 才提炼」。它会把 AER 变成
一个只会记住成功案例的库。

### 问题

失败轨迹有价值吗？

### 最终方案

`ExperienceKind` 三值：`SUCCESS` / `RECOVERY` / `FAILURE`，共用**同一个**
`Experience` 模型、同一套生命周期、同一张表。禁止
`verified_success == false → 什么都不记录`。

失败经验回答的是：

> 「历史上哪些动作已经证明**没有**解决该症状？」

而不是「应该这样做」。

### 理由

- brief §2 明确禁止丢弃失败。Agent 误判成功（`Run.status = SUCCESS` 但
  `verified_success = false`）是 AER 最有价值的信号之一：它是唯一能改进 Agent
  自我判断能力的数据。丢弃它等于永远学不到这一点。
- 三个独立模型（`SuccessfulExperience` / `RecoveryExperience` /
  `FailureExperience`）会让存储、状态机、仓储、检索全部乘三，而三者结构完全
  相同，只有字段读法不同。用一个 `kind` 判别字段即可。
- 「只记成功」还有一个隐蔽代价：检索时无负样本，"什么不可行" 只能靠 Agent 自己
  试错重新发现。

### 重新评估触发条件

若某类经验长出**真正不同**的字段与生命周期（例如 Workflow 化后的操作序列），
再拆模型；仅仅是语义不同不足以拆分。

---

## D-031 kind 由运行事实决定，Provider 只能建议

**时间**：2026-09-16
**里程碑**：M5

### 背景

Distiller 可能是一个语言模型，它倾向于顺着 Agent 自己的叙述走——而「Agent 以为
自己成功」正是最需要被纠正的情形。

### 问题

谁来判定这条经验属于 SUCCESS / RECOVERY / FAILURE？

### 最终方案

由 `aer/experience/classify.py` 的**纯函数**按优先级判定：

```text
1. required verification 失败        → FAILURE
2. Run 未以 SUCCESS 结束             → FAILURE   （含 ABORTED / PARTIAL_SUCCESS）
3. 有成功的 Recovery                 → RECOVERY
4. 其余                              → SUCCESS
```

Provider 的 `kind` 字段只是**建议**：与系统判定不一致时，
记录到 `metadata["provider_suggested_kind"]`（使覆盖可审计），并被忽略。

两个容易误读的分支：

- **未验证的成功仍是 SUCCESS**（不是 FAILURE）：无人检查 ≠ 检查失败。缺失的信任由
  `outcome_verified = false` 和状态停在 `DISTILLED` 承载。
- **RECOVERY 由轨迹形状决定，不要求验证**：修复确实发生了，因为没有验证器就丢弃
  它，等于丢掉最有价值的证据（D-030）。

### 理由

- 答案已经记录在案：`Run.status`（Agent 声明）与 `verified_success`（独立验证，
  M4）。再让模型判一次，等于用一个观点替换一个记录。
- 不一致必须**留下痕迹**而不是静默被覆盖：Provider 判断力的偏差本身就是数据。

### 重新评估触发条件

若出现第四种 kind（如 `PARTIAL`）需求，需先改 brief 与 `agent.md`，而不是让
分类函数自行推断。

---

## D-032 FAILURE 允许 root_cause / solution 为空；且不承载 solution

**时间**：2026-09-16
**里程碑**：M5

### 背景

Schema 若要求 `root_cause` / `solution` 非空，Distiller 就必须为「未解决的问题」
编造一个根因和方案，否则写不进去。

### 最终方案

1. `root_cause` 与 `solution` 均为**可空**；未解决失败的合法形态是两个 `None`。
2. 对于 `FAILURE`，**`solution` 与 `recommended_workflow` 一律不落库到对应字段**：
   Provider 若提供，则移动到 `metadata["provider_solution_hypothesis"]` /
   `metadata["provider_recommended_workflow"]`，字段本身留空。
3. `root_cause` 对 FAILURE **保留**：它本来就是假设（D-033），而失败分析最需要它。

### 理由

- 编造根因比缺失根因更糟：缺失是「不知道」，编造是「伪造知识」，且会被后续
  Retrieval 当作事实使用。
- §4 的红线是「已排除方案不得成为 recommended solution」。失败记录一旦携带
  `solution`，M6 检索就会把未验证的猜测当成答案呈现——这正是「把失败学成成功」。
  结构化拦下比依赖调用方读 `has_verified_solution` 更可靠。
- 信息没有丢失：假设原样保存在 metadata 中，未来可以在**标注为假设**的前提下
  被利用。

### 重新评估触发条件

若出现「运行失败但某次修复在别处被验证成功」的场景，需要引入逐字段的证据标注
（例如 `solution_verified`），而不是重新放开 `solution`。

---

## D-033 Run Verification ≠ Experience Verification

**时间**：2026-09-16
**里程碑**：M5

### 背景

`Run.verified_success` 说明「任务目标是否达成」。经验里的
`root_cause` / `solution` 是**因果陈述**：「切换凭证解决了问题」。

### 问题

Run 验证通过，能否证明经验里的因果解释成立？

### 最终方案

不能，本轮不假装它可以。

- `outcome_verified` 只表示**结果**有独立证据：
  - SUCCESS / RECOVERY：所有 required 验证通过；
  - FAILURE：确有 required 验证失败（**Agent 自己宣布失败不算**——与 M4 拒绝
    Agent 自评同一原则）。
- `root_cause` 明确文档化为**假设**，并提供派生判断 `has_verified_solution`
  （`status is VERIFIED and kind is not FAILURE and solution`）回答「这条经验是否
  带着有证据支持的解决方案」。

### 理由

- M4 的验证是**结果级**的：它检查页面上的 H1 数量，不检查「为什么以前是 0」。
  把结果验证当作因果证明，是把观察升级成解释。
- 一次成功的修复也可能只是碰巧在同一时间生效（缓存过期、并发任务、重试时序）。
  真正的因果验证需要干预实验或多次对照，属于后续阶段。
- 用派生属性而不是新列，是因为它可以从既有字段重新计算，不需要额外写入路径。

### 重新评估触发条件

当同一 claim 积累了足够多次复用数据（M7）后，可以用「按该经验操作的成功率」作为
因果证据的一部分；届时仍需区分「相关性支持」与「因果证明」。

---

## D-034 一条 Experience 可以来自多个 Run

**时间**：2026-09-16
**里程碑**：M5

### 背景

同一个问题会出现很多次。若每次新建一条 Experience，库会被十条几乎相同的记录
填满；若在 `experiences` 上放一个 `source_run_id` 列，又只能记录第一次。

### 最终方案

独立的 `experience_sources` 关联表（联合主键 `(experience_id, run_id)`，两侧
`ON DELETE CASCADE`）。三条规则：

```text
同一 Run 重复 Distill        → 返回已有 Experience，不新建（幂等）
不同 Run，dedup_key 相同      → 附加 Source，不新建
不同 Run，dedup_key 相同但已有记录 DEPRECATED → 新建一条
```

补充：合并时若新 Run 的**结果**有证据（`outcome_is_verified`），可把停在
`DISTILLED` 的记录提升为 `VERIFIED`；**从不降级**，也不改写 claim 本身。

### 理由

- 「一次偶然事件」到「多次真实执行支持的知识」的演化，正是 Experience Store 相对
  日志的价值所在；来源表是唯一能表达它的结构。
- 不合并进 `DEPRECATED`：撤回必须是有意义的。新证据若悄悄挂到已撤回的知识上，
  撤回就失去了作用。
- 允许提升而不允许降级：新证据只能加强 claim。若首个 Run 未被验证，而后续一个被
  验证的 Run 支持同一 claim，把它继续留在 `DISTILLED` 等于丢弃自己最强的证据。
- 合并**不改写** claim（title / problem / solution / dedup_key 不变），也**不动**
  `updated_at`：`updated_at` 表示知识本身的变化，新来源由 source 行记录。

### 重新评估触发条件

当「多条经验都指向同一根因」出现时（相似但不同 claim），需要聚类而非合并；
那属于 M6 Retrieval 的排序问题。

---

## D-035 本轮不做 Embedding Dedup

**时间**：2026-09-16
**里程碑**：M5

### 背景

去重有三种做法：完全匹配、语义相似（Embedding）、模糊字符串匹配。

### 最终方案

只做**确定性指纹**：`dedup_key = sha256(kind + NFKC(casefold(domain/title/problem)))`，
规范化仅包含 NFKC、casefold、空白折叠三项。punctuation 剥离、词干化、同义词
折叠、Embedding **一律不做**。

指纹索引化但**不设唯一约束**。

### 理由

- 误合并的代价与漏合并**不对称**：漏合并多一行（M6 排序可以处理），误合并会把
  证据挂到无关的 claim 上，让库比没有库更糟。规范化越激进，误合并风险越高。
- 因此需要「可疑相似 → 保留两条」，而唯一约束会让规范化过度时**直接拒绝**合法
  数据——数据库不该替业务做这种判断。
- Embedding 会引入向量依赖与模型版本漂移，属于 brief §56 明确禁止的范围；而且它
  解决的是**排序**问题，不是**身份**问题。

### 重新评估触发条件

M6 引入检索后，相似度应当用于**排序与提示**（例如「可能已有相关经验」），而不是
自动写入路径上的自动合并。

---

## D-036 DistillationProvider 不绑定模型厂商

**时间**：2026-09-16
**里程碑**：M5

### 背景

本轮目标是验证 Experience 架构，不是 Prompt Engineering（brief §25）。

### 最终方案

`DistillationProvider` 是 `Protocol`，只要求 `name: str` 与
`distill(evidence) -> ExperienceCandidate`。AER 不 import 任何模型 SDK、不读任何
API Key、不知道任何厂商的请求格式。随包提供的唯一实现是
`CallableDistillationProvider`（包装一个普通函数）。

Provider 在 `AER(...)` 构造时注入；**未配置时 `distill_run` 大声抛出
`DistillationError`**，而不是静默跳过。

### 理由

- 离线可测：本里程碑所有架构保证（kind 由事实决定、崩溃不留半条数据、幂等、
  来源合并）都用「Provider 是一个 Python 函数」验证。需要联网才能断言的话，
  这些保证一条都不可靠。
- 未配置必须大声失败：静默跳过会让「没有提炼」看起来像「没有值得提炼的东西」，
  部署错误被伪装成业务结论。
- `name` 进入 Protocol 而非可选：每条 Experience 都记录产出它的 Provider，可以
  省略的溯源信息最终一定会被省略。

### 重新评估触发条件

接入真实模型时，Prompt、超时、重试与成本控制留在 Provider 实现内；若出现需要
AER 参与的通用需求（例如 token 计量），再考虑扩展 Protocol。

---

## D-037 冻结模型 + 唯一状态机入口；confidence 固定 0.0

**时间**：2026-09-16
**里程碑**：M5

### 背景

`Experience` 有 18 个字段与 8 个状态的组合空间。如果 `status` 可以被赋值，
状态机就只是「文档建议」。

### 最终方案

1. `Experience` 与 `ExperienceSource` 设为 **frozen**（Pydantic
   `frozen=True`）：直接赋值 `status` 会抛 `ValidationError`，唯一的修改路径是
   `transition_to()` / `mark_verified()`，返回**新对象**并写入
   `metadata["last_transition"]` 审计项。
2. `confidence` 本轮**固定 0.0**；Provider 的 `confidence_hint` 保存在
   `metadata["confidence_hint"]`，`root_cause_confidence` 同理。
3. `reuse_count` / `success_count` / `failure_count` **不建列**。

### 理由

- frozen 让「状态转换必须由代码控制」（brief §10）成为类型系统层面的保证，而不是
  约定。`_replace` 走构造器而非 `model_copy(update=...)`，避免绕过校验打洞。
- confidence 需要复用数据才能校准（brief §38）。现在编一个公式会得到一个看起来
  权威、实际无依据的排序键，M6 会直接用它排序——比留空更危险。0.0 是诚实的
  「尚未校准」，而且有一条测试固定「hint 不会变成 confidence」。
- 三个统计列是 `experience_usage` 的产物，而该表尚不存在。放了列却没人写，读出来
  的 0 会被理解成「从未被复用」——一个静默的错误结论。它们与 `experience_usage`
  一起在 M7 落地。

### 重新评估触发条件

- M7 引入 `experience_usage` 后，按 TASKS.md Task 7.5 的公式计算 confidence，并
  同批加入统计列与自动晋升规则。
- 若 frozen 带来的拷贝开销成为瓶颈（需先有测量），再考虑可变模型 + 受控 setter。

---

## D-038 镜像不含数据：SQLite 只存在于持久化卷

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

AER 的状态就是一个 SQLite 文件（`aer.db`）。Docker 镜像默认是只读分层文件系统，
容器删除后其可写层一并消失。

### 问题

如果数据库路径落在镜像内（例如 `/app/data/aer.db`），会得到这样一条链路：
容器正常运行 → 数据看上去正常 → `docker compose down` / 重建容器 → **数据全部消失**，
而中间没有任何一步报错。

### 候选方案

1. 把 `data/` 留在镜像里，靠"不要删除容器"的纪律维持；
2. 用 Docker named volume（命名卷）承载数据；
3. 用 bind mount（绑定挂载）把宿主机目录挂到容器内。

### 最终方案

采用方案 3，且四个持久目录（`/data` `/artifacts` `/knowledge` `/backups`）全部是
**显式 bind mount**，其来源变量使用 `${AER_HOST_*:?}` 形式——变量缺失时 compose
直接报错退出，而不是静默创建匿名卷。

### 理由

- 镜像必须是**可丢弃**（disposable）的：任何时刻删掉重建都不应有任何损失。这是
  "镜像即制品"能成立的前提。
- named volume 的数据藏在 `/var/lib/docker/volumes` 下，备份与人工恢复都要先想清楚
  名字；bind mount 的路径就是文档里写的路径，出事时人找到文件的速度不一样。
- `${VAR:?}` 守卫针对的是最隐蔽的一种失败：compose 在来源变量为空时**不报错**，而是
  创建一个匿名卷。数据从此写进一个没有名字的地方，直到有人 `docker system prune`。

### 重新评估触发条件

- 若未来 AER 需要多副本共享存储 → SQLite 本身就不再合适，届时讨论的是换存储引擎，
  而不是换挂载方式；
- 若单机磁盘成为瓶颈 → 先评估归档与保留策略，再考虑对象存储。

---

## D-039 Embedded Runtime 不伪造常驻服务

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

"部署一个项目"的默认剧本是"起一个服务、加一个 `/health`、让编排系统托管它"。
AER 是嵌入式运行时：没有 HTTP 层，没有监听端口，没有后台 worker。

### 问题

为了让 `docker compose up -d` 看起来正常，要不要加一个空转进程（例如 `sleep infinity`）
或一个只为返回 200 的 HTTP 端点？

### 候选方案

1. 加 `sleep infinity`，让容器"常驻"，符合运维直觉；
2. 加最小 FastAPI + `/health`；
3. 不加常驻进程：镜像的默认命令打印版本后立即退出；compose 只用于提供**受控执行环境**。

### 最终方案

方案 3。

- 镜像不设 `ENTRYPOINT`，默认 `CMD` 打印 `aer-runtime <version>` 并退出；
- compose 的 `aer-runtime` 服务 `restart: "no"`，不声明 `ports`、`command`、`healthcheck`；
- 主要运行方式是 `docker compose run --rm aer-runtime <command>`。

### 理由

- 一个不做事却常驻的进程是**负债**：它会出现在监控里、会被报警、会诱导下一个人去
  "优化"它，而它唯一的产出是"我还活着"。
- 默认命令打印版本后退出，让 `docker compose up` 天然无害（而不是看起来成功），同时
  `docker run <image>` 直接就是最小冒烟测试。
- 冒烟测试命名为 `smoke_test.sh` 而不是 `healthcheck.sh`：后者暗示"可以反复轮询的
  健康端点"，而 AER 没有这样的东西。**名字错了，运维理解就会错。**

### 重新评估触发条件

- 出现真实的常驻需求（常驻 Agent worker、定时任务、MCP 服务）时新增服务定义，
  届时为它单独写 healthcheck，而不是给 AER 套一个。

---

## D-040 镜像内 editable 安装：迁移脚本的定位机制决定镜像布局

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

`aer.storage.migrations._repository_root()` 通过**从包所在位置向上查找
`alembic.ini`** 来定位迁移脚本目录。这是 M2 引入 Alembic 时就存在的机制。

### 问题

Docker 里最常规的做法 `pip install .`（非 editable）会把包装进 `site-packages`，
于是从包向上查找永远找不到 `alembic.ini`。结果是**镜像能导入 AER、能打开数据库，
但一迁移就失败**。

这一点不靠推断，而是实验确认的：

```text
$ cp -r aer /tmp/fake-site-packages/ && PYTHONPATH=/tmp/fake-site-packages python -c "..."
StorageError: Cannot locate alembic.ini: AER's migration scripts are missing from the installation.
```

### 候选方案

1. 非 editable 安装 + 把 `alembic.ini`/`migrations/` 复制到 site-packages 的父目录；
2. 非 editable 安装 + 为 `_repository_root()` 增加 `AER_PROJECT_ROOT` 环境变量回退；
3. editable 安装（`pip install --editable .`），让包与 `alembic.ini`、`migrations/`
   同处 `/app` 下；
4. 把 `migrations/` 移进包内（`aer/migrations/`）。

### 最终方案

方案 3：镜像里 `WORKDIR /app`，复制 `pyproject.toml` / `README.md` / `alembic.ini` /
`aer/` / `migrations/` / `scripts/`，然后 `pip install --editable .`。

### 理由

- `_repository_root()` 的 docstring 本来就写明了它"支持源码检出与 editable 安装"——
  editable 是这个项目**已经设计过**的部署形态，不是为 Docker 临时发明的。
- 方案 1 依赖解释器的目录结构（`/usr/local/lib/python3.12/`），并且让镜像里出现
  两份源码；方案 2 需要改存储层代码并引入第二套路径解析规则；方案 4 要移动大量文件，
  且与本轮"不改业务代码"的边界冲突。
- editable 安装保持了**单一源码副本**：`/app/aer` 既是包也是被导入的模块，不会出现
  "改了 A 处的文件、生效的是 B 处"。

### 重新评估触发条件

- 若未来把 `alembic.ini` + `migrations/` 变成包数据（`importlib.resources` 读取），
  非 editable 安装即可成立，届时本决策与 `_repository_root()` 一起重写；
- 若镜像体积成为真实问题（需先测量），再评估多阶段构建与 venv 的取舍。

---

## D-041 生产服务器不构建源码：Build Once, Deploy Same Artifact

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

传统部署有两种做法：在服务器上 `git pull` + 安装依赖，或在 CI 构建制品再分发。

### 问题

"服务器上跑的是什么"能否被唯一确定？如果服务器自己拉代码构建，那一台机器的
`pip` 缓存、系统库版本、甚至 Python 小版本都会改变结果——同一 commit 在两台机器
上产出不同行为，而 `current.env` 记录的 commit 是相同的。

### 候选方案

1. 服务器 `git clone` + `pip install`（或 `docker build`）；
2. CI 构建镜像并推送，服务器只 `docker pull`。

### 最终方案

方案 2，并在部署脚本中**显式禁止**方案 1 的行为：
`deploy.sh` 里不出现 `docker build` / `git clone` / `pip install`（有测试固定这条规则）。

### 理由

- 制品唯一性：生产上跑的字节来自 CI 那一次构建，可以用镜像 digest 指认。
- 服务器因此不需要 Python、不需要编译工具链、不需要仓库写权限——攻击面与依赖面同时缩小。
- 回滚因此变得简单：回滚就是换一个镜像引用，没有"用旧代码重新构建一次"的不确定性。
- 服务器上唯一被同步的是几个 host 侧脚本与 compose 文件，它们由 CI 从**同一个
  已验证 commit** 推送过来。

### 重新评估触发条件

- 若出现必须在目标机器编译的场景（例如依赖本机 CUDA 版本），那属于不同的交付模型，
  需要新的决策记录，而不是放宽这条规则。

---

## D-042 迁移在容器内执行；备份在迁移之前，且由镜像自身完成

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

部署顺序里有三件事相互纠缠：何时备份、用谁来备份、用谁执行迁移。

### 问题

若在宿主机上装一套 Python + Alembic 来迁移，就会出现"宿主机代码版本 ≠ 容器运行版本"：
宿主机用旧 revision 迁移新库，或反过来，都能造成极难排查的 schema 漂移。

### 候选方案

1. 宿主机装 Python/Alembic 执行迁移；
2. 容器内执行迁移；
3. CI 远程执行迁移（在 runner 上连生产数据库）。

### 最终方案

方案 2。备份同样在容器内执行，因此 `deploy.sh` 的实际顺序是：

```text
pull 镜像  →  备份  →  迁移  →  冒烟  →  写 current.env
```

注意这与常见直觉（"先备份，再拉镜像"）有一处顺序差异，是有意的。

### 理由

- 迁移必须由**将要运行的那个镜像**执行：容器里的 Alembic 与容器里的模型代码必然同源，
  这是"宿主机版本漂移"的根治办法。
- 备份放在 `pull` **之后**，是因为运行备份脚本需要一个 Python 环境——用它即将部署的
  镜像来跑最自然（该镜像刚刚拉取、必然存在），从而让服务器完全不需要 Python。
  **真正的安全不变量是"备份严格早于迁移"，而不是"备份早于拉取"**：拉取对数据库是只读的，
  不可能损坏数据；迁移才会。
- 备份脚本是页级复制，与被备份库的 schema 无关，所以用新镜像备份旧 schema 的库是安全的。

### 重新评估触发条件

- 若未来出现"必须在迁移前进行业务层面的数据导出"的需求 → 那属于数据迁移策略，
  应新增独立的 pre-migration hook，而不是继续堆在 `deploy.sh` 里；
- 若备份耗时增长到影响发布窗口 → 评估增量备份或快照方案（先测量）。

---

## D-043 SHA 镜像标签是唯一生产标识；不发布 `latest`

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

镜像仓库惯例是同时发布 `latest`。生产部署必须能回答"线上是哪个 commit"。

### 问题

`latest` 的语义是"最后一次推送者"，与"应该部署哪个版本"没有任何关系。一旦生产
依赖它，回滚就必须先回答"上一个 latest 是什么"，而这个问题在 `latest` 被覆盖后
已经不可回答。

### 候选方案

1. 发布 `sha-<commit>` 与 `latest` 两个标签，生产用 `sha-*`；
2. 只发布 `sha-<commit>`。

### 最终方案

方案 2（brief §12 允许"额外生成 latest"，属可选项）。并且：

- `deploy.sh` 与 `rollback.sh` 都**拒绝** `:latest` 或无标签的镜像引用；
- CI 的 `workflow_dispatch` 允许手工部署已有镜像，但同样拒绝 `latest`；
- "当前版本"记录在 `/srv/aer/deploy/current.env`，包含 `AER_IMAGE` / `AER_GIT_SHA` /
  `AER_PREVIOUS_IMAGE` / `AER_DEPLOYED_AT` / `AER_BACKUP`。

### 理由

- 每多一个可变名字，就多一条"部署了非预期版本"的路径；`latest` 买到的唯一便利是
  "少打几个字"，代价是回滚时信息缺失。
- 手工重新部署一个已存在的镜像时**不重新构建**：同一 commit 重建会产生不同字节，
  那就破坏了不可变标签的意义。

### 重新评估触发条件

- 若出现需要"人类快速试用最新版"的场景 → 在非生产环境发布 `edge` 之类的可变标签，
  生产仍然只认 `sha-*`。

---

## D-044 不自动 `alembic downgrade`：镜像回滚与数据库恢复分离

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

`rollback.sh` 的设计必须回答：回滚时数据库怎么办？

### 问题

数据库降级（downgrade）由**新版本**定义的逆向迁移执行，作用在**新版本已经写入**的
数据上。它几乎无法在真实数据上预先验证，而 AER 的 M5 之后会产生不可再生数据
（从真实执行中提炼的经验）。一次错误的降级会同时摧毁代码与数据。

### 候选方案

1. 回滚时自动 `alembic downgrade` 到目标镜像的 revision；
2. 回滚只切镜像；不兼容时由人显式从备份恢复。

### 最终方案

方案 2。

- `rollback.sh` 中不出现任何 `alembic` 命令（有测试固定）；
- 切换后对旧镜像跑冒烟测试；失败则**拒绝更新 `current.env`** 并打印人工恢复步骤；
- `--force` 仅用于"已手工恢复数据库、只需固定镜像版本"的场景。

### 理由

- 自动降级把"应用回滚"这一低风险操作与"数据库改写"这一高风险操作绑在了一起，
  使得一次简单的版本回退可能变成数据事故。
- 拒绝更新 `current.env` 是刻意的：一个记录了"当前版本"却与实际不符的文件，会在
  下一次事故中把人引向错误的判断，比没有记录更糟。
- `restore_sqlite.py` 刻意要求显式的 `--source-backup` 与 `--target`，不提供"恢复最新"
  的捷径：选择恢复点是运维决定，工具不应替人猜。

### 重新评估触发条件

- 若未来某个 revision 被证明可以安全降级（有测试、有真实数据演练）→ 可以为那个具体
  revision 提供显式命令，但**不改变**默认行为。

---

## D-045 单机 Docker Compose，不引入 Kubernetes

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

目标环境是一台 Linux 服务器（`bwg-06`）。

### 问题

编排层（orchestration）该选什么？

### 候选方案

1. Kubernetes（+ Helm / Terraform / Ansible）；
2. Docker Compose 单机。

### 最终方案

方案 2。`deploy/compose.yaml` 描述一个服务、四个挂载、一组环境变量，主要运行方式是
`docker compose run --rm`。

### 理由

- AER 是**单进程嵌入式**运行时 + 一个本地 SQLite 文件。SQLite 的写锁语义就意味着
  它天然是单节点存储；上编排系统不会带来水平扩展能力，只会带来一层必须维护的抽象。
- K8s 解决的是"多节点调度、滚动发布、自愈"——在只有一个节点、且进程本身是一次性命令
  的场景里，这些能力都无处施展。
- 复杂度是负债：本轮的目标是让链路**可重复执行**，不是让它可编排。

### 重新评估触发条件

- **真实**出现多节点需求（例如并发跑多个 Agent worker，且需要调度）→ 届时先换掉
  SQLite（那才是真正的瓶颈），再讨论编排；
- 出现"同一台机器上需要多副本隔离"的需求 → 优先评估资源限制与多实例目录隔离。

---

## D-046 最小配置模块：`AER_*` 环境变量 + 生产环境绝对路径校验

**时间**：2026-09-16
**里程碑**：Infrastructure

### 背景

AER 此前只通过构造函数参数接收数据目录（`AER("./data")`），没有任何环境变量读取。
容器化要求进程能从环境得知数据位置。

### 问题

要不要引入 pydantic-settings / dynaconf 之类的配置框架？

### 候选方案

1. 引入完整 Settings 框架（分层来源、校验 DSL、`.env` 解析）；
2. 新增 `aer/config.py`：一个 frozen dataclass + 一个读取函数，只处理部署必需变量；
3. 只改 `AER.__init__` 的默认值，让它读环境变量。

### 最终方案

方案 2。变量集合刻意很小：

```text
AER_ENV                 development | production
AER_DATA_DIR/AER_DB_PATH  数据目录 / 精确的库文件（复用 alembic.ini 既有约定）
AER_ARTIFACT_DIR / AER_KNOWLEDGE_DIR / AER_BACKUP_DIR
AER_LOG_LEVEL / AER_BACKUP_RETENTION
```

并且三条确定性校验：

1. 空字符串视为未设置（`AER_DATA_DIR=` 是容器写到错误目录的常见成因）；
2. 日志级别必须是标准级别之一，否则启动即失败（拼错的级别会被静默忽略）；
3. `AER_ENV=production` 时所有路径必须为绝对路径——相对路径会相对**镜像的工作目录**
   解析，于是"看起来正常"地写出第二个空数据库，这是 §49 警告的那类损失。

"绝对"按**宿主机语义或 POSIX 语义任一成立**判断：被校验的是部署目标（Linux 容器）的
路径，而 `pathlib.Path("/data").is_absolute()` 在 Windows 上是 `False`。用宿主语义去
判断容器路径会拒绝正确的配置，进而教人削弱这条校验。

### 理由

- 配置框架的能力（多来源优先级、嵌套模型、热重载）在本项目一个都用不到，却会引入
  一个必须学习的抽象和一条必须跟随的依赖。
- `AER.DB_PATH` 复用 `AER_DB_PATH`：这个变量在 M3 就已是 `alembic.ini` 与
  `migrations/env.py` 的约定。部署再发明一个名字，等于制造"CLI 与运行时指向不同文件"
  的机会。
- 未改动 `aer/runtime/`、`aer/verification/`、`aer/experience/` 的任何语义：`AER` 的
  构造函数签名不变，`aer.config` 是**新增**的旁路。

### 重新评估触发条件

- 变量数量或校验规则显著增长（例如出现按租户的配置）→ 那时再评估框架；
- 若出现必须在**进程启动时**校验配置的常驻服务 → 校验位置从脚本上移到服务入口。

---

## D-047 只读文件系统上的完整性校验：`immutable=1` 回退，但非空 `-wal` 必须拒绝

**时间**：2026-09-17
**里程碑**：Infrastructure（灾难恢复演练轮）

### 背景

灾难恢复演练按最安全的姿态挂载备份：`-v /srv/aer/backups:/backups:ro`。恢复立刻失败：

```text
[restore] FAILED: /backups/aer-....db is not a readable SQLite database: unable to open database file
```

文件存在、权限正确、`PRAGMA integrity_check` 在可写挂载下是 `ok`。

### 问题

AER 的数据库**始终带 WAL 标记**——模式写在文件头（`write_version=2` / `read_version=2`），
不是连接时选项。而一个只读连接即使在 `mode=ro` 下，也要创建 `-shm` 共享内存索引；在只读
文件系统上内核会拒绝，SQLite 报 `SQLITE_CANTOPEN`。

于是出现一个荒谬的结论：**持有备份最安全的方式（只读介质、快照、只读副本）恰好是恢复工具
做不到的那一种。** 而灾难现场需要的正是它。

### 候选方案

1. **要求备份目录以可写方式挂载。** 代价：为了读一份备份而必须允许写它。而且可写挂载下
   `mode=ro` 依然会在备份旁创建 `-wal` / `-shm`，这正是模块文档警告的"陈旧预写日志会被
   重放到恢复后的库上"的同族文件。
2. **换成 `mode=ro` + `immutable=1` 无条件使用。** 代价：`immutable=1` **按定义跳过预写
   日志**。若文件旁真有非空 `-wal`，读到的就只是主文件——一个关于陈旧页面的自信 `ok`。
3. **先 `mode=ro`，失败时检查 `-wal`，再决定是否用 `immutable=1`。**

### 最终方案

方案 3。回退**有条件**：

```text
mode=ro 打开失败且错误是 "unable to open database file"
  ├── 旁边存在非空 -wal  → 拒绝，并说明"已提交页面可能仍在预写日志里"
  └── 否则（无 -wal 或 0 字节）→ 用 mode=ro&immutable=1 重试
```

`-wal` 为 0 字节是合法且常见的形态（干净关闭后 SQLite 会留下空壳），把它当危险会拒绝掉
正常情况。非空的则以拒绝告终。

### 理由

- 判据是**可本地验证的事实**（文件是否存在、是否非空），不是对调用场景的假设。
- 只读挂载下不存在写入者，因此不存在"正在产生的 WAL"；只要当前没有非空 `-wal`，主文件
  就是全部状态。
- 拒绝比误判便宜：灾难恢复中一个错误的好消息，比一个明确的坏消息危险得多。
- 回退只在前一种打开**失败**时发生，所以常规路径（可写挂载）的行为一字未改。

### 事后补充：第一版只修了「校验」

第一版把这个回退实现在 `integrity_check` 里，即只覆盖了**校验**。恢复流程另有一处打开源的
代码（`sqlite3.connect(backup)`，读写），于是出现了一份自相矛盾的状态：`--dry-run` 从只读挂载
返回 0，真正的恢复仍然失败。**部署后验证在真实服务器上抓到了这个组合**——单元测试没抓到，
因为测试里没有只读文件系统。

修法不是再补一处 `try/except`，而是把「怎么以只读方式打开一个库」收敛成**唯一入口**
`open_read_only()`，让所有读取路径（校验、sidecar 元数据、恢复的源）都用它。同时它返回前
会强制读一次（`PRAGMA schema_version`）：`sqlite3.connect` 是惰性的，不强制读就会把失败推迟到
`Connection.backup` 中途，并留下一个 0 字节的目标文件。

**教训（比缺陷本身重要）**：一处不变式存在于两个调用点时，修一个不算修；而且**"修好了"必须
由目标环境验证**，本地测试通过只说明本地路径通过。

### 重新评估触发条件

- 若出现真正的并发写入者（多进程 / 常驻服务），只读校验的前提失效，需要改为带一致性检查
  的快照读取；
- 若恢复目标改为远程对象存储（S3 等），则"文件系统只读"这一前提消失，可重新简化。

---

## D-048 恢复的结论必须来自恢复本身：调用方不得再次打开目标

**时间**：2026-09-17
**里程碑**：Infrastructure（灾难恢复演练轮）

### 背景

演练在恢复目录里发现了 `aer.db-shm`（32768 字节）与 `aer.db-wal`（0 字节）——**紧挨着刚
恢复好的数据库**。而 `restore_sqlite.py` 的模块文档开头就写着：留下属于旧库的预写日志会
在下次打开时被重放到新库上，产生无声损坏。清理逻辑明明存在，而且放在了"最后一次打开之后"。

### 问题

不变式写对了，但放错了层级。`restore_backup()` 内部确实做到了"清理晚于最后一次打开"，
可真正的最后一次打开在**调用方**：

```text
restore_backup()
  ├── 校验备份
  ├── 清除陈旧 -wal/-shm
  ├── Backup API 复制（两个连接均已关闭）
  ├── 清除
  ├── _require_intact(target)   ← 只读打开，造出 -shm
  └── 清除                      ← 此刻是干净的
main()
  └── integrity_check(target)   ← 又只读打开一次，-shm/-wal 回来了，再没人清
```

Windows 上这条路径掩盖了现象（干净关闭时 SQLite 会自己删掉副文件），所以本机测试通过，
而在 Linux 容器里必然复现。

### 候选方案

1. `main()` 里在重新校验之后再清一次。仍然脆弱：不变式散落在两个函数里，下次有人加一次
   打开就又破了。
2. **让 `restore_backup()` 返回它已经算出的完整性结论，调用方不再打开目标。** 最后一次
   打开因此**只能**发生在 `restore_backup` 内部，紧邻最后一次清理。

### 最终方案

方案 2。`restore_backup(...) -> tuple[Path, str]`，第二个元素是 `integrity_check` 的结论；
`main()` 直接用它填 `target_integrity`。

### 理由

- 把"最后一次打开"变成**结构上不可能发生在别处**，而不是靠注释提醒。
- 结论本来就已经算出来了，重算一次的唯一效果是产生副作用——重复计算没有信息增量。
- 回归测试必须走 CLI 层。既有测试全部调 `restore_backup()`，而缺陷活在两个函数**之间**，
  层次不对的测试永远看不到它。已验证：新测试在修复前的代码上失败。

### 重新评估触发条件

- 若恢复流程未来拆成多个进程（例如恢复与校验分为两个镜像），该不变式需要改为显式的
  "清理步骤"，并各自负责自己的退场路径。

---

## D-049 凭据的存活窗口止于拉取：部署流程显式 `docker logout`

**时间**：2026-09-17
**里程碑**：Infrastructure（灾难恢复演练轮）

### 背景

零长期凭据的方案（见 D-046 与部署手册 §6.4）让 CI 用本次 job 的 `GITHUB_TOKEN` 登录服务器
再拉取私有镜像。原注释写着"该 token 会自己过期，**这恰好够用**"。

演练做例行检查时发现：上一次部署（数小时前）的 token **仍然躺在**服务器
`/root/.docker/config.json` 里——`ghs_` 前缀的 GitHub App 安装令牌，用户名 `Wike-CHI`，
写入时间正是那次拉取的时刻。

### 问题

"会自己过期"被当成了"不需要清理"。但在它过期之前，那是一台公网可达主机上的常驻凭据：
没人使用、没人观察、也没有任何机制会在它被使用时报警。**过期的承诺不是清理。**

### 候选方案

1. 保持现状，依赖过期。代价如上述。
2. 服务器上改用长期 `read:packages` PAT。更糟：把短期问题换成永久问题，且与"零长期凭据"
   的方向相反。
3. **部署流程在拉取完成后登出。**

### 最终方案

方案 3。部署 job 增加一步 `docker logout ghcr.io`：

- `if: always()`——失败的部署同样不能把凭据留在那里；
- 位置在 `Deploy over SSH` **之后**，因此拉取必定已经完成（登出先于拉取会破坏它正在
  收尾的那次部署）；
- 登出后**校验** `/root/.docker/config.json` 里确实不再出现该 registry，而不是假设
  `docker logout` 成功了；
- 校验失败只发 `::warning::` 而**不**让步骤失败。清理不该有能力把一次成功的发布变成失败，
  也不该掩盖已经发生的失败。

`rollback.sh` 的行为不变：拉取失败即退回本地镜像（D-044 的方向），因此凭据消失不影响
事故现场回滚。

### 理由

- 凭据的存活窗口从"直到它自然过期"缩短为"拉取镜像所需的那几秒"，且这是**由流程保证**的，
  不依赖任何人的自觉。
- 清理与创建放在同一个 job 里，谁创建谁负责。
- 用 `grep` 校验而非信任退出码：`docker logout` 在没有凭据时也会成功返回。

### 重新评估触发条件

- 若服务器需要长期自主拉取能力（例如自动扩容、多机部署），必须重新引入凭据，并同时引入
  轮换机制与到期告警；
- 若 GHCR 支持匿名拉取（镜像公开），本决策整体作废。

---

## D-050 演练证据是逻辑等价，不是校验和；演练工具只读由构造保证

**时间**：2026-09-17
**里程碑**：Infrastructure（灾难恢复演练轮）

### 背景

演练必须回答"备份恢复出来的东西和源是不是一样"。最直觉的做法是比较 SHA-256。

### 问题

实测：生产库与它的备份，**大小相同、表相同、每张表的内容摘要都相同**，但校验和不同——
相差 **3 个字节**，全部落在 SQLite 文件头（`first_differing_offset=27`，即 change counter，
`2` vs `1`）。若用校验和当判据，这份**完全正确**的备份会被判为"不一致"。

反过来，校验和一致也推不出内容一致吗——不，但"不一致"这个词会把一个正常的备份说成坏的，
而这正是灾难恢复中最不该出现的误报。

### 最终方案

1. 演练的等价性判据是**逻辑等价**：schema 全文 + 每张表按 `rowid` 排序后的内容摘要；
   字节差异只用于**定位**（差几个字节、差在哪里），不用于下结论。
2. 记录保全（第 11 节）比较**逐字段值**，不只是行数。
3. 三个演练工具（`drill_facts` / `drill_compare` / `drill_seed`）随镜像发布，其中前两个
   **只以 `mode=ro` 或 `immutable=1` 打开数据库**，并在输出里记录自己用的是哪种模式。
   它们会被指向生产库，所以"不可能写入"必须是构造属性，不能是代码纪律。
4. 生产库当前 0 行，因此第 11 节的记录保全改在**演练沙箱内**播种的数据上验证，并在文档里
   写明为什么换数据源——`0 == 0` 证明不了任何东西。

### 理由

- 判据必须能区分"文件不同"与"数据不同"。SQLite 的文件头计数器让这两件事天然分离，
  用校验和就是把它们混为一谈。
- "不可能写入"由构造保证（只读 URI + 只读挂载）比"我们不会写"更可靠：后者依赖每个未来的
  维护者。
- 输出里记录打开模式，是因为**证据要能说明自己是怎么得来的**。一个不说明读取方式的数字，
  在事故复盘时无法被信任。

### 重新评估触发条件

- 若 SQLite 之外引入其他存储格式，等价性判据需要各自定义，"逻辑等价"不能直接照搬；
- 若生产库开始有真实数据，第 11 节应改为优先在生产记录上验证，沙箱播种退化为补充手段。

---

## 模板（后续决策请复制此结构）

```text
## D-XXX 标题
**时间**：
**里程碑**：

### 背景
### 问题
### 候选方案
### 最终方案
### 理由
### 重新评估触发条件
```
