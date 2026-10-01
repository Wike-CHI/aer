# Codex Adapter（M8.1）

把 Codex CLI 的生命周期事件接进 AER。用**公开 hook 接口**，不 fork、不打补丁、
不新建服务。

- 被测版本：**codex-cli 0.155.1**（`@openai/codex@0.155.1`），测量日期 2026-09-20
- 实现：`aer/adapter/codex/`
- 探针：`scripts/probe_codex_hooks.py`
- 决策：`docs/DECISIONS.md` D-085..D-094

---

## 1. 实测得到的结论（不是文档抄来的）

本轮所有结论来自**实际安装的二进制与它真实投递的 payload**：

| 事实 | 证据 |
| --- | --- |
| 12 个 hook 事件存在 | 二进制字符串 + 本机真实 `hooks.json` |
| 声明位置是 `~/.codex/hooks.json` | 本机存在可用的 hooks 文件（memmy 钩子） |
| `config.toml` 的 `[hooks]` 表也能解析 | `--strict-config` 接受 |
| hook 默认受信任门禁 | `TrustHooks` / `SetHookTrusted`；`--dangerously-bypass-hook-trust` |
| **`exec` 模式确实分发 hook** | `hook: SessionStart` / `hook: UserPromptSubmit` |
| `SessionStart.source` ∈ startup/resume/clear/compact/fork | 二进制枚举 + 实投 |
| `SessionEnd` 只有 `reason`，观测值为 `other` | 实投 payload |
| 失败的 hook **不重试**，会话继续 | 故意让 hook 退出码 7 后观测 |
| `SessionEnd` / `Interrupt` 的 timeout 被压到 3 秒 | `warning: clamping ...` |
| **程序路径不能加引号** | 加引号 → Codex 静默启动失败；去掉引号 → 成功 |

### 一个真实存在的坑

```text
"command": "\"C:\\...\\python.exe\" \"C:\\...\\hook.py\" SessionStart"   ← 失败，且没有任何提示
"command": "C:\\...\\python.exe C:\\...\\hook.py SessionStart"           ← 成功
```

加引号时 Codex 报 `hook: X Failed` 或干脆沉默，**看起来就像 hook 没触发**。
本机真实的 memmy 配置用的是 `node "带引号的参数"` —— 程序不加引号、参数加引号。

---

## 2. 覆盖矩阵（**这是交付物的一部分**）

`python -m aer.adapter.codex.hook --print-coverage`

证据等级只有三种：

```text
CAPTURED      真实 payload 已捕获，映射已对照它检查过
CONTRACT_ONLY 映射存在，但从未见过真实 payload
MISSING       既没观测到，也没有映射
```

### exec 模式（2026-09-20 用 gpt-5.6-luna 真实回合捕获）

| 事件 | 等级 | 捕获到的字段 |
| --- | --- | --- |
| `SessionStart` | ✅ CAPTURED | session_id, transcript_path, cwd, hook_event_name, model, permission_mode, source |
| `UserPromptSubmit` | ✅ CAPTURED | 同上 + turn_id, prompt |
| `PreToolUse` | ✅ CAPTURED | 同上 + tool_name, tool_input, tool_use_id |
| `PostToolUse` | ✅ CAPTURED | 同上 + tool_response（**字符串**） |
| `Stop` | ✅ CAPTURED | session_id, turn_id, cwd, last_assistant_message, stop_hook_active, model, permission_mode |
| `SessionEnd` | ✅ CAPTURED | session_id, transcript_path, cwd, hook_event_name, reason |
| `PermissionRequest` | ❌ MISSING | 探针会话形状没有产生 |
| `PreCompact` / `PostCompact` | ❌ MISSING | 同上 |
| `SubagentStart` / `SubagentStop` | ❌ MISSING | 同上 |
| `Interrupt` | ❌ MISSING | 需要交互式会话 |

### interactive 模式

**全部 MISSING**——TUI 需要终端，自动化探针无法覆盖。

### 为什么写这么多 MISSING

一个没有工具事件的 Run，和一个"这次没用工具"的 Run，在数据里**完全一样**。
把缺口写出来，才能区分这两种情况（第 37-38 节）。

## 3. 安装

### 3.1 写 hooks.json

`~/.codex/hooks.json`（用户级；实测项目级 `.codex/hooks.json` 不会被 exec 采用）：

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python -m aer.adapter.codex.hook", "timeout": 30 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python -m aer.adapter.codex.hook", "timeout": 30 } ] }
    ],
    "PreToolUse": [
      { "hooks": [ { "type": "command", "command": "python -m aer.adapter.codex.hook", "timeout": 30 } ] }
    ],
    "PostToolUse": [
      { "hooks": [ { "type": "command", "command": "python -m aer.adapter.codex.hook", "timeout": 30 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "python -m aer.adapter.codex.hook", "timeout": 30 } ] }
    ]
  }
}
```

注意：

```text
只装这 5 个事件。其余 7 个即便装上也会被记为 IGNORED（还没有被观测到的映射）。
如果 python 不在 Codex 进程的 PATH 上，写绝对路径 —— 但**不要给程序路径加引号**。
```

### 3.2 让 hook 通过信任门禁

Codex 默认要求 hook 被显式信任。两种做法：

```text
交互式：在 TUI 里批准（TrustHooks）
脚本化：codex exec --dangerously-bypass-hook-trust   （审计/CI 用，名字已经说明了代价）
```

### 3.3 告诉 hook 数据写到哪里

```bash
export AER_DATA_DIR=/path/to/aer-data      # 与 AER 其它部分一致（AER_DB_PATH 优先）
python -m aer.adapter.codex.hook --print-coverage    # 自检
```

### 3.4 验证

```bash
echo '{"hook_event_name":"SessionStart","session_id":"probe-1","cwd":"/tmp"}' \
  | python -m aer.adapter.codex.hook --print-delivery
```

预期：退出码 0，stderr 出现 `Codex session probe-1 started (new, source=None); awaiting a
prompt before opening a run`。**不产生 Run**——这是设计（见下）。

---

## 4. 生命周期语义

```text
SessionStart      不建 Run（payload 里没有 task 字段，D-091）
UserPromptSubmit  用 prompt 的脱敏摘要建 Run（同一个 session 后续 hook 一律 resume）
PreToolUse        → TOOL_CALL
PostToolUse       → TOOL_RESULT；tool_response 是字符串，没有结果字段，
                     因此 success 缺席、也不写 ERROR（D-097）
其他十一个事件     显式 IGNORED 并计入 coverage（D-092）
SessionEnd        **唯一** terminal authority（D-087）
```

`SessionEnd` 的状态映射：

```text
reason ∈ {complete, completed, success, done}          → SUCCESS
reason ∈ {error, failed, failure}                      → FAILED
reason ∈ {interrupt, interrupted, aborted, cancelled}  → ABORTED
其他（观测到的 "other"）                                → INCONCLUSIVE
                                                          + metadata.codex.outcome_stated=false
```

### `"other"` 是正常值（D-099），所以 Run 不该借别的状态

实测确认：**正常完成的会话也发 `"other"`**。于是 Codex 从不声明结果，这不是异常路径，
而是唯一路径。Adapter 因此需要 AER 有一个能表达"结束了、没人说怎么样"的状态 ——
那就是 `INCONCLUSIVE`（D-100）。它不声称成功，也不声称失败；一个独立的 required 验证
仍然可以把它确立为 verified success。

### 已知限制

```text
Codex Run 仍然不会自己变成 verified success —— 它不能，因为 Codex 没有声明。
但它现在**可以**被验证确立：AER 的验证路径一旦证明结果，它就是一个 verified success，
kind 是 SUCCESS（有修复则是 RECOVERY），而不是 FAILURE。
在此之前它不能产出经验：没有声明也没有验证时，AER 没有可写的 kind（D-100）。
```

---

## 5. 四条边界（可被测试证伪）

```text
Codex 说 "Done. All tests pass."  ≠ Verification     无 external_verification 能力
工具调用成功                       ≠ Experience 被采用  无 explicit_adoption_signal 能力
hook 执行成功                      ≠ Experience 已注入  不写 record_injection
会话结束                           ≠ 任务成功          未声明 → INCONCLUSIVE
                                                        （也不是失败；可被验证确立）
```

每条都有对应的**否证式测试**（`tests/adapters/codex/test_codex_adapter.py`）：

```text
跑完整会话后 get_verifications() == []
record_usage_signal(ADOPTED) 抛 AdapterCapabilityError，落库仍是 UNKNOWN
跑完整会话后 experience_usage.count() == 0 且 retrieval_sessions.count() == 0
失败的 tool 之后换一个 tool 成功 → 不产生 RECOVERY_START/RECOVERY_RESULT
未验证的 INCONCLUSIVE 会话      → should_distill=False、kind=None（不伪造 FAILURE）
已验证的 INCONCLUSIVE 会话      → verified_success=True、kind=SUCCESS（不会是 FAILURE）
```

---

## 6. 升级 Codex 之前

```bash
python scripts/probe_codex_hooks.py --output coverage.json
```

- **不需要登录，也不消耗 token**：默认用一个不存在的模型名，让 run 在模型调用处失败，
  而 hook 在此之前已经触发。
- 需要工具级 payload 时加 `--use-auth`（会消耗额度，取决于你的账号与网络）。
- 退出码非 0 表示**会话级 hook 没有再被观测到**——此时 Adapter 的映射已经不能假定成立。

```bash
# 免费分支：只覆盖会话级三个事件
python scripts/probe_codex_hooks.py

# 有网络与额度时：覆盖工具级事件
python scripts/probe_codex_hooks.py --use-auth --mode exec
```

---

## 7. 本轮明确不做

```text
不改 AER Core 的 EventType（第 16 节）
不读 rollout JSONL（第 41 节；里面可能含完整 prompt / 隐藏推理 / 巨量工具输出）
不把 notify 当作完整数据源（第 42、D-086）
不建 FastAPI ingestion server / Kafka（第 61 节）
不 fork / patch Codex（第 43 节）
不做 Claude / Cursor / DSH Adapter（属 M8.2–M8.4）
```
