# 技术部建站经验：登记、采集、验收与复用

## 范围与当前事实

本期复用 Claude Code Adapter。原适配器按 prompt/turn 分 Run；本入口按明确登记的
`project_id + task_id + tool session` 分 Run，同一任务的重复 prompt 不创建新 Run。
不同工具 session 可以关联同一任务，收尾需明确列出相关 Run。
Codex 仍用原适配器，工具结果与错误能力不能等同 Claude。DSH bridge 的默认入口仍然
是探针用 RecorderSink，未接 RuntimeSink；不要将它的固定上下文当作真实经验。

Claude `PostToolUse` 没有明确成功结果时保持未知，`PostToolUseFailure` 记录结构化失败。
`Stop` 不结束任务；`SessionEnd` 只产生 INCONCLUSIVE。Hook 不提炼、不重建索引、不推断
根因或成功。模型候选不等于事实，验证与经验状态沿用 AER 原有规则。

## 1. 安装和登记一个试点项目

在 AER 独立 Python 3.12+ 环境安装 `pip install -e ".[dev,mcp]"`；Linux/macOS 如需
NeuG 检索使用 `pip install -e ".[dev,mcp,knowledge]"`。Windows 无 NeuG wheel，必须
在 Linux/macOS 的实例上进行索引与真实检索验收。不要把 AER SDK 安装进 blogo 进程。

创建负责人维护的 JSON 配置，放在项目工作树之外（例如 `/srv/aer-config/site-a.json`）。
以下均为待替换示例，不是已配置的客户、站点或命令：

```json
{
  "project_id": "site-a",
  "root": "/srv/sites/site-a",
  "data_dir": "/srv/aer-data",
  "task_id": "acf-repair-001",
  "stage": "repair",
  "customer_id": null,
  "site_id": null,
  "checks": {
    "php-syntax": {
      "argv": ["php", "-l", "functions.php"],
      "scope": "syntax",
      "description": "PHP syntax only; no assertion about frontend behavior"
    },
    "acceptance": {
      "argv": ["/srv/checks/site-a-acceptance", "--task", "acf-repair-001"],
      "scope": "task",
      "description": "Owner-maintained WP readback and rendered-page acceptance",
      "timeout": 60
    }
  }
}
```

所有路径必须绝对路径，ID 必须稳定、只含字母数字点横线下划线。数据实际写入
`data_dir/project_id/`，绑定该项目根目录。不要复用项目 ID，也不要登记重叠项目目录。
嵌套 Git 仓库不会被父目录 Hook 收集。无可信客户/站点标识时保留 null。
`checks` 是运行代码的受信配置，只允许负责人维护；不要让模型上传 argv、URL 或 passed。
先读建站交付规范的字段映射、操作说明和验收清单，实际编写与本任务相符的检查器。
检查脚本可以执行 pytest、WordPress 回读或浏览器检查；本功能不假装自带站点凭据。

```bash
python -m aer.projects.cli --project /srv/aer-config/site-a.json install
```

安装合并 `.claude/settings.local.json`，保留原 Hook 与权限配置，首次备份到
项目数据目录的 `claude-settings-before-aer.json`，重复安装同配置不重复增加 Hook。
`settings.local.json` 应保持本地忽略。
不改变 Claude 信任或工具许可。启动新 Claude session，完成登记任务；换 task_id 时必须
启动新 session。否则拒绝采集，防止旧 session 的迟到事件误归新任务。

安装时预初始化 SQLite，Hook worker 超时 8 秒，设置层 timeout 10 秒；错误只向 stderr 输出类型，不输出原始载荷，
外层返回 0 让开发继续，失败子进程的退出码写入诊断。启动 worker 前先写独立的
`capture-pending/<task_id>/<delivery>.json`，确认返回后清除本次标记；取消或崩溃留下未完成标记。
能写入时在 `capture-gaps/<task_id>.json` 留异常标记；写盘本身不可用时只能
依靠 stderr 诊断。Hook 未被工具调用的事件无法检测，必须先实际运行探针确认覆盖面。
这不是无损采集承诺。现有账本是 claim→apply→mark_applied 的 at-most-once：重复投递不
重复计数，未应用 claim 不会重放。gap、pending 或未应用 claim 会阻止收尾和 SDK/MCP 提炼。
进程在开始写标记前即被杀死、或工具根本没有调用 Hook，不能从本地数据推断捕获完整。
不要删除 gap 来制造完整性；修复采集后用新 task/session 重新采集受影响工作。

## 2. 结束任务、保存验证、提炼候选

```bash
python -m aer.projects.cli --project /srv/aer-config/site-a.json runs
```

先提交项目实际修改，取完整 Git SHA。收尾要求当前 checkout 与 SHA 相同且干净。
它记录起始版本、dirty 标记、最终验收版本与证据路径，不把截断工具摘要当作完整代码。
大型页面截图、报告可由负责人检查脚本保存在独立制品目录；候选 metadata 可带其引用。

根据可审查的 Run 证据准备候选 JSON，或由模型生成后人工核对（只是提议）：

```json
{
  "domain": "wordpress",
  "title": "ACF 返回格式需与模板读取方式一致",
  "problem": "填写实际观察到的问题",
  "failed_attempts": ["填写确实发生的失败尝试"],
  "root_cause": "待证据支持的原因假设",
  "solution": "填写实际修复方式",
  "avoid": ["填写适用限制"],
  "metadata": {"applicability": "unknown", "limitations": ["仅验证本次配置"]}
}
```

```bash
python -m aer.projects.cli --project /srv/aer-config/site-a.json closeout \
  --run RUN_ID --revision FULL_COMMIT_SHA --candidate /srv/review/candidate.json
```

可重复 `--run` 明确关联多个 Run。先核对所有项目/task 归属，再执行负责人配置的检查；
不接受 passed 参数。所有检查完成后才写一个整套验证结论，避免部分 PASS 后检查器崩溃
造成误成功。结果逐项保留 scope、退出码、时间、代码版本与脱敏诊断文件及 SHA256。
诊断最多 64 KiB，截断明确标记；完整验收制品应由检查器另存引用。
`syntax`、`fields`、`frontend` 的证明范围不同；没有覆盖整个任务的 `task` 检查时，
整套验证只作 optional evidence，不能产生 verified success。
实际失败保留 FAILURE 经验且不会带可信 solution；检查器异常记录 ERROR、不造失败判决。
已有验证的 Run 不覆盖重验，修复后用新 Run。候选经 AER 原有提炼、分类、去重与状态流转，
本入口不直接赋值 VERIFIED。

## 3. 下一次任务复用

在支持 NeuG 的系统显式执行索引投影（不在 Hook/查询内执行）：

```bash
python -m aer.projects.cli --project /srv/aer-config/site-a.json project-index
```

项目入口固定使用 `<data_dir>/<project_id>/knowledge`，不同项目不共享进程当前目录的索引。
配置 MCP 时同时设置绝对路径 `AER_DATA_DIR=/srv/aer-data/site-a`（SQLite 所在目录）
和 `AER_KNOWLEDGE_DIR=/srv/aer-data/site-a/knowledge`（该项目索引），由
`python -m aer.mcp` 提供 stdio，或使用现有带 Bearer 认证的 HTTP 服务。
若部署环境已设置 `AER_DB_PATH`，必须同时改为该项目的绝对 `aer.db` 路径或取消该覆盖；
它优先于 `AER_DATA_DIR`。`domain` 只用于相关性检索，不构成客户权限隔离。
新任务调用 `aer_search_experiences` 即可获取有限长度、有来源的历史建议和失败提醒。
详见 [固定检索契约](SEARCH_CONTRACT.md)。查询不创建虚假 Run，不记录注入/采用。
确实需要跟踪注入时，用原有 `aer_retrieve` 绑定真实运行中的 Run；只有客户端已将返回内容
放进模型上下文，才调用 `aer_acknowledge_injection`。没有采用信号就保持 UNKNOWN。

本地 SQLite **没有自动同步公司中央库**。公司共享必须显式部署独立实例，并审核脱敏语料、
访问权限与来源。运营只接收正式字段映射/使用说明及整理到 `wordpress-operations` 的经验；
不能直接把技术 trace、代码、终端日志放入 blogo。AER 无客户级隔离，首期用绑定试点客户的
专用实例或确认可共享的独立语料库，不能全库检索后过滤冒充隔离。

## 验收入口与限制

`pytest tests/projects tests/knowledge/test_pure_search.py` 使用临时目录、合成 Hook 和真实
子进程检查，绝不写真实经验库。`test_real_index_from_captured_checked_run` 贯穿错误采集→
修复观察→实际检查→原生提炼→NeuG 检索→来源回溯；无 NeuG 时明确 skip。
这是隔离验收夹具，不是客户站点的成功案例。

真实工具现场应先运行 `python scripts/probe_claude_hooks.py` 检查当前 CLI 的实际 Hook。
2026-11-04 在隔离 Git/PHP renderer 项目使用 Claude Code 2.1.317 实测：错误出现→
工具修复→SessionEnd 保持 INCONCLUSIVE→配置的 PHP 检查通过→原生提炼。该链路无
未应用事件、capture gap 或 pending marker。来源 Run 为
`8a275f4d-44ad-4d28-b6ff-0a5cc387c4ee`，验收提交为
`a8f363bad971a19471737179766a74c5cc79b32d`，经验为
`f4704f71-7d14-450f-ac06-9a04e9eb8bdb`（原生 VERIFIED/SUCCESS；没有人工修改状态）。
随后在 WSL Ubuntu 24.04 / NeuG 0.2.0 对该隔离库显式投影，查询
`PHP heading title escape` 返回 ready 和同一来源 Run。查询前后 Run=1、usage=0、
retrieval_sessions=0，未制造采纳或成功指标。原始证据保留于工作区
`.tmp/aer-live-probe/closeout3.json` 与 `retrieval3-evidence.json`，不提交原始执行轨迹。
此前两个失败探针的 gap 保留，未清除或用于可信提炼。

这是实际 CLI 与实际 PHP 检查的隔离演示，尚非客户 WordPress 站点验收；真实 WordPress
回读、页面检查、负责人检查配置和公司集中共享仍需现场验收。Windows 原生无 NeuG
wheel；上述真实检索在 WSL 完成。模型上下文注入与采用仍未知，不以检索成功代替。
