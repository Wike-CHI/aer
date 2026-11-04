# Claude 开发机 → 中央 AER

范围：登记项目的 Claude Code 原生 Hook，通过本地 outbox 和显式 SSH 同步进入现有中央 AER；不上传/替换数据库，不导入本地经验状态。Hook 没有网络或模型调用。同步不会证明任务成功。

## 开发机安装

使用包含本功能的 AER Python 环境，按 [项目采集说明](WORDPRESS_PROJECTS.md) 安装项目 Hook（原有 Hook 合并保留）。项目配置放在仓库外的绝对路径，至少包含：

```json
{
  "project_id": "junj-cn",
  "task_id": "search-filter-escape-001",
  "stage": "repair",
  "root": "D:/work/junj",
  "data_dir": "D:/aer-data",
  "sync_source_id": "jh-dev-01"
}
```

`root` 和 `data_dir` 必须替换为真实绝对路径。设备标识由管理员分配、在服务器登记；不能由模型自行选择租户或权限。首次任务 Hook 前开启 `sync_source_id`，不追溯伪造过去事件。每个新任务换稳定 task_id；同一个 outbox 不允许换设备。项目/任务/工具会话/首次代码版本保留在 metadata，中央 Run ID 与本地不同，通过返回映射关联。

```sh
python -m aer.projects.cli --project /absolute/project.json install
```

在登记目录启动 Claude Code 执行真实任务，退出会话产生 SessionEnd 后同步。Stop 只代表回合停止，不等于会话结束。未提供工具结果保持未知；完整源码与大型材料使用 Git revision/独立制品，不从截断摘要复原。

```sh
python -m aer.projects.sync push --project /absolute/project.json --ssh-host bwg-06
```

Windows 与 Linux 使用相同 Python CLI；SSH alias 由操作者配置，密钥和主机指纹遵循已有 SSH 信任。不新增公网端口或服务口令。默认容器为 `aer-aer-mcp-1`，可用 `--container` 指定现有容器。无自动定时上传；需显式执行同步或由公司管理的调度器调用。

## 中央服务器登记与验收

将服务器所有者维护的 `/artifacts/central-projects.json` 挂载给现有 AER 容器：

```json
{
  "projects": {
    "junj-cn": {
      "source_ids": ["jh-dev-01"],
      "root": "/artifacts/projects/junj-cn",
      "checks": {
        "search": {
          "argv": ["php", "tests/search-filter.php"],
          "scope": "task",
          "description": "仅验证搜索分类筛选修复，不代表整站验收"
        }
      }
    }
  }
}
```

此处检查与路径是管理员审核的配置，不接收上传数据中的命令。SSH 账户须限制为授权人员；本版本以 SSH 主机账户和 manifest 为信任边界，**设备ID本身不是认证凭据**，拥有该账户执行权限的人可代表 manifest 中登记设备。不可将它宣传成敌对多租户隔离。

管理员把审查过的 Git bundle/checkout 放到登记 root，确认工作树干净、revision 与本次修复一致；容器需安装 git、PHP 等真实检查依赖。源代码上传和生产发布不由 sync 自动执行。检查脚本本身也必须经过审查：执行已提交的脚本仍意味着信任该脚本。

准备 ExperienceCandidate JSON（问题、失败尝试、修复、适用限制），通过标准输入调用：

```sh
docker exec -i aer-aer-mcp-1 python -m aer.projects.sync closeout --manifest /artifacts/central-projects.json --project-id junj-cn --task-id search-filter-escape-001 --source-id jh-dev-01 --revision ACTUAL_COMMIT < candidate.json
docker exec aer-aer-mcp-1 python -m aer.projects.sync project-index
```

验证结论由服务器配置的实际检查生成，保留时间、退出码、输出引用、SHA；候选不携带可信 passed。验收/提炼复用 AER 原生规则，不能指定 VERIFIED。syntax/fields/frontend 与 task 范围不同。失败或异常不生成可信成功；已经验证过的 Run 拒绝重复 closeout，修复需新任务/Run，避免覆盖历史。多 Run 收尾如果中途异常，应检查已落地验证，不能盲目重试整个批次。

完成投影后，下一次任务调用现有 `aer_search_experiences` 纯检索工具。检索不创建 Run、不记 injection/采用；只有实际放入上下文才能记录 injection。中央看板读取同一 AER 数据目录即可看到 Run、验证和经验，无需复制本地 SQLite。

## 传输与故障契约

`aer.claude.sync.v1` envelope 包含 project_id/task_id/source_id/items；每条包含单调 sequence、原始事件身份哈希、脱敏 payload、本地 Run ID、观测时间、项目关联。每批最多100条、2MiB，单条128KiB。字段脱敏与4096字符摘要采用现有 sanitizer；不发送完整 transcript。脱敏不是所有业务机密的保证，管理员应检查数据范围。

本地保留 outbox；服务器以整个条目的 SHA256 保存序列收据。成功 ACK 保存为 last-ack.json；超时后重发同一批不会重复应用。重启恢复已经完整落盘收据；缺序号、同序号内容冲突、设备未登记、跨项目关联、session/Run 改绑均拒绝。服务器串行接收，避免不同流同时修改共享 Runtime。

继承原有 at-most-once 账本，不承诺无丢失自动重放。接收前写 pending，应用后写收据；崩溃可能留下 pending 或锁文件。此时阻断同步及公共 SDK/MCP 提炼入口。操作者应保留原始 outbox、收据、数据库、诊断并对照事件；不能直接清除锁/pending后宣称完整。无法确认完整性则保留旧任务为不完整，重新采集新任务。源 Hook gap/pending/unapplied 同样阻止发送。

## 验证

```sh
python -m pytest tests/projects -q
python -m ruff check aer/projects aer/experience/service.py tests/projects
python -m mypy aer
```

`test_central_sync.py` 全部为隔离临时目录内的合成 Hook 夹具，不能写入真实经验库。真实 Claude/站点演示证据另行记录；本说明不声称夹具通过即真实环境通过。

本地并行 Hook 使用项目级互斥，最多等待4秒，采集进程总上限仍为8秒；超过上限保留 gap，不静默宣称成功。诊断 reason_code 区分 capture_lock_timeout、capture_worker_timeout、event_identity_conflict 等且不输出载荷。当前 outbox 去重扫描已有文件，大任务成本随历史增长；首期按具体任务拆分，超时须作为数据缺口处理。原子文件写入会 flush/fsync，但不承诺机器断电下跨 SQLite 和文件的原子事务。
