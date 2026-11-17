# Experience Loop：首批开发者试用与验证

启动日期：2026-11-17。第一轮目标是让 5 个外部 Agent 项目完成接入、复用经验并
提供持续使用证据。以下业务目标与价格均为待验证假设，不代表已获得用户或回款。

## 产品与用户

一句话定位：把 Agent 的失败、恢复和独立验证证据，变成下一次任务可检索的工程经验。

English: Turn agent failures and verified recoveries into reusable engineering experience.

首批用户选择正在维护工具型 Agent 的独立开发者或 1–10 人团队；他们每周都有真实
多步骤任务，并能提供最近一次重复失败的案例。访谈先问现有处理办法和损失，再演示产品。
第一轮交付是本地 Python Runtime、现有 Adapter 和可重算报告。

## 仓库现状与本轮范围

| 能力 | 启动时状态 | 本轮动作 |
| --- | --- | --- |
| Capture / Verify / Distill / Retrieve / Usage | main 已有实现 | 用真实项目检查接入成本 |
| 分配实验与重算指标 | main 已有 `Experiment` / `compute_report` | 提供 CLI 文本与 JSON 入口 |
| 后台、项目采集、中央同步 | PR #10 → #11 → #12 尚开放 | 单独审查，不能假定 main 已包含 |
| 真实 Agent A/B 效果 | Issue #7 尚待验证 | 学习语料与评估任务隔离，独立验收 |
| 付费托管、多租户、计费 | 未交付 | 由重复使用和付费需求决定是否投入 |

现有实验报告尚不提供 Token 成本、独立 retry 计数、统计显著性或污染检测。
不要把没有采集的指标写成 0，也不要把检索命中当成明确采用。

## 先完成一次可运行的接入

Python 3.12+，从本 PR 所在源码安装。以下命令在仓库根目录执行。

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev,mcp]'
python scripts/run_experiment_pilot.py --data-dir ./data/pilot --tasks 20 --seed 7
python -m aer.cli --data-dir ./data/pilot experiment report pilot
python -m aer.cli --data-dir ./data/pilot experiment report pilot --json > pilot-report.json
```

这条演示不依赖模型或 NeuG。它使用脚本执行器与内存索引，只检查数据流和报告；
差异是脚本预设的，不能作为产品效果证据。报告读取持久化 Run，不重新执行任务、
不创建检索或注入记录；它沿用 CLI 打开 Runtime 的方式，可能执行 schema 迁移。
报告失败时退出码为 1，JSON 写入文件前应检查命令退出码。

需要真实全文检索时，在 Linux 或支持 NeuG wheel 的 macOS 上安装
`python -m pip install -e '.[dev,knowledge,mcp]'`，再执行
`python scripts/install_neug_extensions.py`。Windows 可采集、验证和提炼，
当前 NeuG 检索需使用支持的平台；参见 [可用版本](USABLE_VERSION.md)。

真正的失败→恢复→提炼→检索→消费验收入口：

```bash
python -m aer.demo --output-dir ./demo-results
```

该入口使用真实 NeuG 检索，但仍是本地受控示例，不证明真实 Agent 的收益。
将演示目录与自己的真实项目数据目录分开。接入 SDK 见 README，已有平台可参照
[Claude Adapter 实现](../aer/adapter/claude/)、[Codex Adapter](CODEX_ADAPTER.md) 和
[DSH Adapter](DSH_ADAPTER.md)。

## 把同一实验入口接入真实 Agent

1. 固定 Agent、模型、工具版本、任务集合和判定标准。先在独立学习集提炼经验，
   冻结评估用经验快照，记录代码 SHA、seed 与语料版本；评估期间不回灌测试答案。
2. 用 `Experiment(aer, name="wp-real-v1", seed=7)`，对每个任务调用
   `begin_task(..., unit=stable_task_id)`。相同 unit 总是进入同一臂；当前实现是按
   unit 分配，不是同一任务跨两臂配对。重复 unit 不应被当成独立样本。
3. 两臂执行相同 Agent 代码，统一经 `task.retrieve()` 取经验。对照臂返回 None；
   treatment 返回的文本实际进入上下文后，才通过 `record_injection()` 记账。
   采用与有帮助标签须有显式来源，未知就保留 UNKNOWN。
4. 所有任务使用相同的外部验收；保留失败、未验收、运行中与检索基础设施异常。
   Agent 声称完成不能代替 verifier。分别记录人工介入与工具轨迹。
5. 每臂至少 50 个独立任务后重算报告，并另做样本质量和不确定性分析；达到 50
   本身不保证有统计显著性。当前 CLI 不自动判断“有效”或“通过商业验收”。

```bash
python -m aer.cli --data-dir /path/to/real-store experiment report wp-real-v1 --json
```

`success_rate` 是 Agent 声明 SUCCESS 的比例；`verified_success_rate` 是全臂任务中
满足 Runtime 独立验证规则的比例。两者分母都包括运行中和未验证任务，因此应在
同一任务收尾窗口比较。`verified_success_lift` 是 treatment 减 control 的绝对差，
0.10 表示 10 个百分点。缺少一臂时 lift 为 null。
`assisted_success_rate` 仅是“检索命中且声明成功”的观察值，不代表采用或因果收益。

## 第一周交付与验收

| 工作 | 完成证据 | 启动状态 |
| --- | --- | --- |
| 本地演示和报告入口 | 安装步骤、命令输出、测试与 PR CI | 本 PR 验证 |
| 寻找 15 位候选用户 | 本人维护的项目、具体失败案例、沟通许可 | 待执行 |
| 访谈前 5 位 | 当前排障流程、频率、耗时与最近一次故障 | 待执行 |
| 2 个外部项目接入 | 接入用时、失败步骤、首个验证结果 | 待执行 |
| 1 次七天回访 | 第二次真实使用或停止使用原因 | 待执行 |

候选用户优先来自现有开源项目参与者、有相关问题的开发者和自愿报名者；此文不代表
已经联系任何人。未经同意不采集他们的执行内容，也不自动发送推广消息。

访谈问题：最近一次 Agent 重复犯错发生了什么？现在如何确认任务完成？排障用了
多久？现有 tracing 工具缺了哪一步？愿意拿哪个真实任务试用？一周后还会使用吗？
谈付费时明确说明实际可交付范围，并询问他们愿意为哪项结果付费。

可自行发送的邀请文案：

> 我在做 AER，一个把 Agent 失败与经过验证的恢复过程转成可复用经验的开源工具。
> 正在找 5 位维护工具型 Agent 的开发者，用一个真实任务试接入。我可以陪你完成
> 一次本地安装，重点记录接入耗时与它能否减少重复排障。你最近是否遇到过这种问题？

## 90 天业务关口

| 阶段 | 先验证什么 | 继续条件 |
| --- | --- | --- |
| 第 1–30 天 | 用户能完成接入并获得价值 | 5 个外部项目接入，3 个完成经验闭环 |
| 第 31–60 天 | 用户重复使用并愿意付费 | 3 组可复现实验、2 个真实付费试点 |
| 第 61–90 天 | 产品能持续交付 | 5 个付费用户/组织、至少 1 次实际续费 |

先测试 ¥49/月个人、¥199/月团队的价格接受度，它们是报价假设。当前开源能力仍按
Apache-2.0 使用；订阅仅在相应托管/团队服务可交付时收费。访谈中的“愿意付费”与
真实回款分别记录。托管前先列明事件量、保留期、模型调用承担方和每客户支持成本，
不能按低价承诺无限存储与服务。90 天总支出上限暂定 ¥3,000，每周投入约 12 小时。

每周记录：候选→访谈→安装→首次验证→完整经验闭环→第二周使用→付费→续费。
分母和日期必须保留；不以 Stars 或下载量替代活跃项目。安装、访谈和回款目前都未
验证，不能将目标填成实际数据。首次验证接入耗时目标 10 分钟，必须实测。

如果用户只持续使用独立验证，下一轮收窄为 Verification 工具；如果接入后无人重复
使用，暂停 Cloud 投入，修复接入和价值问题。后台合并、生产部署与付费服务发布按
各自审查结果推进，不能用演示成功替代这些验收。
