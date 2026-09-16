"""Update DEPLOYMENT.md with the verified first-deployment results."""

from __future__ import annotations

import pathlib
import sys

PATH = pathlib.Path("docs/DEPLOYMENT.md")
text = PATH.read_text(encoding="utf-8")

if "真实部署记录" in text:
    print("already patched")
    raise SystemExit(0)

OLD = """## 14. 本轮已验证 / 未验证

诚实说明边界，避免有人误以为某条链路已经跑过。

| 项 | 状态 |
| --- | --- |
| `pytest` / `ruff` / `mypy` | ✅ 本机通过（Linux 上由 CI 复跑） |
| 备份 / 恢复 / 保留策略 | ✅ 自动化测试覆盖（含损坏备份、陈旧 WAL、幂等恢复） |
| Alembic 全新库 + schema parity | ✅ 通过 |
| `Dockerfile` 构建、容器冒烟 | ⚠️ **未在本机执行**：本机无 Docker Engine。由 `ci.yml` 在 `ubuntu-latest` 上执行 |
| `docker compose config` 校验 | ⚠️ 同上，由 CI 执行 |
| SSH 部署到真实服务器 | ⚠️ **未执行**：无生产凭据。链路已实现，首次真实部署请按第 7 节走一遍并核对本文档 |"""

NEW = """## 14. 真实部署记录与验证状态

### 首次部署（已完成）

```text
目标服务器 : bwg-06 (65.49.202.129, Linux x86_64)
镜像       : ghcr.io/wike-chi/aer:sha-f0215c4a2d1c3e5bf22a3f8b1595c1359e4b6492
数据库     : /srv/aer/data/aer.db（Alembic revision 0004，integrity_check = ok）
CI 运行    : actions/runs/35078175077（push 到 main 触发，全部 job 通过）
```

容器内对**生产库**的冒烟结果（`docker run ... smoke_test.py`）：

```text
[ok] runtime.version      aer 0.5.0, python 3.12.14
[ok] config.resolve       environment=production, db_path=/data/aer.db
[ok] directories.writable data=/data artifact=/artifacts knowledge=/knowledge backup=/backups
[ok] database.integrity   /data/aer.db -> ok
[ok] database.revision    0004 (head 0004)
[ok] runtime.open         runs=0 experiences=0
all 6 checks passed
```

### 状态表

| 项 | 状态 |
| --- | --- |
| `pytest` / `ruff` / `mypy` | ✅ 本机与 CI 均通过 |
| 备份 / 恢复 / 保留策略 | ✅ 自动化测试覆盖（含损坏备份、陈旧 WAL、幂等恢复） |
| Alembic 全新库 + schema parity | ✅ 通过 |
| `Dockerfile` 构建、容器端到端（建库→播种→备份→恢复→冒烟） | ✅ CI（ubuntu-latest）通过 |
| `docker compose config` 校验 | ✅ CI 通过（含"空 AER_IMAGE 必须被拒绝"的反向校验） |
| SSH 部署到真实服务器 | ✅ 已执行并验证（见上） |
| 镜像不含数据 | ✅ 实测 `/app/data` 不存在，生产库只在 bind mount |
| 镜像可回答"线上是哪个 commit" | ✅ `docker inspect` 的 `org.opencontainers.image.revision` = 该 commit |
| 备份分支（迁移前备份） | ⚠️ 首次部署时数据库尚不存在，按设计跳过；第二次部署起生效 |
| 回滚到上一版本 | ⚠️ 尚未有"上一版本"，第二次部署后才有可回滚目标 |
| 真实故障恢复演练 | ⚠️ 未做（建议在正式使用前安排一次） |"""

assert text.count(OLD) == 1, text.count(OLD)
text = text.replace(OLD, NEW, 1)

PATH.write_text(text, encoding="utf-8", newline="\n")
assert "真实部署记录" in PATH.read_text(encoding="utf-8")
print("updated docs/DEPLOYMENT.md")
sys.exit(0)
