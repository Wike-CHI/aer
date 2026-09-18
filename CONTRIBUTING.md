# 贡献指南

AER 的目标是**把 Agent 的真实执行轨迹变成可追踪、可验证、可复用的经验**。这个目标决定
了它的几条红线：执行事实与验证结论必须分开，失败的运行也是资产，数据库历史必须能一路
升到 head。下面先讲怎么跑起来，再讲改什么会被拒绝。

开发边界与优先级的完整说明在 [`agent.md`](agent.md)；架构决策与理由在
[`docs/DECISIONS.md`](docs/DECISIONS.md)。**动手前先读这两份**——绝大多数"显然更好"的
方案，那里已经讨论过并给出了不做的理由。

---

## 开发环境

要求 **Python >= 3.12**。用一个虚拟环境，不要往系统 Python 里装：

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -e ".[dev]"
```

`[dev]` 会带上 `pytest`、`ruff`、`mypy`、`pyyaml`。最后一个不是可有可无的：基础设施测试
**真的解析** `deploy/compose.yaml` 与 workflow 文件，而不是对它们做文本匹配。

运行时依赖：`pydantic` 2.x、`SQLAlchemy` 2.x、`Alembic` 1.13+、`neug==0.2.0`。
`neug` 是精确锁定的——知识层的正确性建立在 NeuG 0.2.0 的**实测行为**上，理由见
`docs/DECISIONS.md` D-055。

> 在 Windows 上 `neug` 没有 wheel，知识层的**引擎相关**测试会被跳过。这不是失败，
> 但也意味着**本机绿色不等于 CI 绿色**：涉及引擎行为的改动必须在 CI（或 Linux 容器）
> 上确认。这一轮的真实教训见 [`docs/TASKS.md`](docs/TASKS.md) 的 M6 记录。

---

## 运行测试

仓库里实际存在的命令，就这四条（CI 跑的就是它们）：

```bash
pytest
ruff check .
ruff format --check .
mypy aer/
```

测试套件**不访问网络**，也用不到 Docker。

有 Docker 时，基础设施部分还能再验一层：

```bash
docker build -t aer:local .
docker run --rm aer:local                        # 打印版本后立即退出
cp deploy/env.example deploy/.env
docker compose -f deploy/compose.yaml config
```

改动 shell 脚本时 `shellcheck --severity=warning scripts/*.sh`（CI 上装了，本地缺就跳过）。

---

## 数据库迁移

**这是本仓库最硬的一条规则。** Schema 由 Alembic 管理，`Base.metadata` 只是
autogenerate 的输入，不是创建 schema 的手段（`aer/` 里不允许出现 `create_all`，
有验收脚本用 AST 检查它）。

### 规则

1. **`migrations/versions/` 下的已发布 revision 不允许改写。**
   不要改 `upgrade()` / `downgrade()` 的内容，不要改 `revision` / `down_revision`，
   不要重排、不要重命名文件。别人手里的数据库已经按那个文件迁移过了——改它等于同时
   存在两个都叫 `0004` 的不同 schema。
2. **改 schema 必须新增 revision**，用
   `alembic revision --autogenerate -m "<描述>"`（它会自动跑 `ruff format` +
   `ruff check --fix`，见 `alembic.ini` 的 `post_write_hooks`）。禁止手工改库、
   禁止删库重建。
3. **新增自定义列类型时**，在 `migrations/env.py` 的 `render_item` 里补映射。
   revision 文件**不得 import 应用包**：它必须在那份代码被改名、移动或删除之后仍然可执行。
4. **必须验证升级路径**：`alembic upgrade head` 要在**空库**上跑通，也要在**旧版本的库**
   上跑通，且旧数据逐字段保全。
5. **必须保护历史数据库向 head 升级的能力。** 旧库不是测试夹具，是真实资产。
   播种旧 revision 的库时用 `scripts/drill_seed_revision.py`——**绝不能用当前 Runtime
   去写一个旧 revision 的库**，因为 `AER(...)` 构造时就会把它迁移掉。
6. **绝不 `alembic downgrade`。** 本项目没有自动化降级：`scripts/rollback.sh` 只切镜像，
   数据库恢复是另一个显式决定（`docs/DECISIONS.md` D-044）。

### 相关测试

- `tests/storage/test_migrations.py` —— 空库到 head、原地采纳无 `alembic_version` 的旧库、
  旧 revision 原地升级保数据、以及 **schema parity**（迁移产出 vs `Base.metadata` 的 DDL）。
  加了 revision 就要把 `HEAD_REVISION` 常量和 `EXPECTED_TABLES` 一起更新——那是刻意的，
  让"加了一条迁移"成为一个有意识的改动，而不是静默通过。
- `tests/infra/test_alembic_resolution.py` —— 用**真实 CLI 子进程**验证
  `AER_DATA_DIR` / `AER_DB_PATH` / `-x db_path=` 的优先级，以及 CLI 与运行时指向同一个文件。
- `tests/infra/test_drill_seed_revision.py` / `test_smoke_script.py` —— 版本感知的播种与冒烟。

---

## 打包：改动后 wheel 必须真的能用

`pip install aer-runtime` 是一条**受支持**的路径，所以打包不是"顺手改改 `pyproject.toml`"：

```bash
python -m pip install --upgrade build twine
python -m build                 # sdist →（从 sdist）wheel
python -m twine check dist/*
```

`python -m build` 默认**先出 sdist，再从这个 sdist 出 wheel**——所以任何只存在于工作区、
没进 sdist 的文件（`alembic.ini`、`migrations/`、`LICENSE`、`setup.py`、`MANIFEST.in`）
都会在 wheel 里消失，而失败会出现在用户第一次 `AER(...)` 的时候，不是构建的时候。

迁移脚本在 wheel 里的位置由 `setup.py` 的 `build_py` 在构建期决定（把仓库的
`alembic.ini` + `migrations/` 复制进 `aer/_migrations/`，理由见 `docs/DECISIONS.md` D-064）。
因此**任何涉及迁移脚本、`setup.py`、`MANIFEST.in`、`pyproject.toml` 的改动**都要额外
验证一次"装出来的包能建库"：

```bash
python -m pip install --no-deps --target /tmp/aer-wheel dist/*.whl
PYTHONPATH=/tmp/aer-wheel python -c "
import aer; print(aer.__file__)
from aer import AER
AER('/tmp/aer-wheel-data').close()          # 构造即迁移；不抛就说明包内迁移脚本可用
"
```

**注意 `--target` 不够。** 如果当前环境里装过 editable 版本，`sys.meta_path` 里的
`_EditableFinder` 会让 `import aer` 悄悄回落到源码树，于是这个冒烟测试验证的是工作区而不是
制品。测试脚本要先把它摘掉，并断言 `aer.__file__` 落在 target 目录里。

---

## Pull Request

- **一个 PR 只解决一个明确问题。** 顺手的重构请单独开一个。
- **新功能必须带测试。** 没有测试的功能等于没有完成。
- **bug fix 尽量补 regression test** —— 让同一个 bug 不可能悄悄回来。
- **不得通过删除或弱化测试来绕过失败。** 测试红了先判断：是实现错了，还是测试的前提变了？
  后者要在 PR 里说明理由，而不是把断言删掉。
- **公共 API 改动要说明兼容性。** 公开面是 `aer/__init__.py` 的 `__all__`；
  `aer.runtime` **不重导出 `AER`**（会造成循环并拖入整个应用图），用 `from aer import AER`。
- **schema 改动必须带 migration**（见上）。
- **安全相关改动要说明风险**，并按 [`SECURITY.md`](SECURITY.md) 的边界判断是否需要
  调整那条"设计限制"。
- 提交信息用中文写清楚**为什么**。这个仓库的历史以"决策 + 理由"为单位，不是以"改了什么"为单位。

CI（`.github/workflows/ci.yml`）在 PR 上跑质量门禁、全新库的 Alembic CLI、镜像构建与
容器端到端（迁移 → 播种 → 备份 → 恢复 → 冒烟）。`main` 上的 `deploy.yml` 会重跑同一套门禁。

---

## 架构原则（改代码前请先同意这几条）

1. **执行成功 ≠ 验证成功。** `run.success()` 只是 Agent 的自述；`verified_success` 是派生
   判断，**不落库**。两者永远不能合并成一个字段。
2. **执行事实不得被验证结果覆盖。** 验证失败不改写 `Run.status`。Tool 失败也不自动把 Run
   标成 FAILED——Agent 可能自己恢复。
3. **失败也是有效经验来源。** `kind ∈ {SUCCESS, RECOVERY, FAILURE}` 共用一个 `Experience`；
   `verified_success == false` 不是丢弃理由。但 `FAILURE` **不承载 `solution`**。
4. **未验证的方案不得升级为可信经验。** 生命周期 `RAW → DISTILLED → VERIFIED → …` 只能经
   `transition_to()` / `mark_verified()` 变更（模型是 frozen 的）；`REUSED` 及以上在
   用量统计落地前**不可达**。
5. **崩溃 ≠ 结论。** 验证器 / 提炼器抛异常时写 `ERROR` + `ErrorRecord`，绝不伪造
   `passed=false`，也不产出半条 `Experience`。
6. **数据库历史必须可迁移**（见"数据库迁移"）。
7. **Embedded / lightweight 是当前设计目标。** 单机、SQLite、同步、确定性代码、低依赖。
   **不要**引入 PostgreSQL / Redis / Kafka / 独立向量库 / 微服务 / K8s——除非有真实瓶颈，
   并且先在 `docs/DECISIONS.md` 里写清楚为什么。
8. **业务层不写 SQL、不持有 `Session`**，一律走 Repository。
9. **枚举只在 `aer/runtime/enums.py`**，一律 `StrEnum`；JSON 载荷走
   `aer/runtime/serialization.py` 的 `JsonValue` / `JsonObject`，编解码只允许出现在
   `aer/storage/converters.py`。
10. **时间用 timezone-aware UTC（`utc_now()`），耗时用单调时钟（`perf_counter()`）。**

---

## 发布

发布由 tag 驱动，不要手工上传制品：

1. 在 `main` 上确认质量门禁全绿；
2. 更新版本号（`pyproject.toml` 的 `version` 与 `aer/__init__.py` 的 `__version__`
   **必须一致**，测试会盯住这一点）；
3. 打 tag `vX.Y.Z` 并创建 GitHub Release（release notes 写**已实现**的能力，
   未实现的一律进 "Known limitations"）；
4. Release 发布会触发 `.github/workflows/publish.yml`，经 PyPI Trusted Publishing（OIDC）
   构建并上传。**不需要、也不允许**在仓库里放 PyPI token。

PyPI 侧的 Trusted Publisher（仓库 / workflow 名 / environment 名）需要人工配置一次，
见 `docs/DEPLOYMENT.md`。
