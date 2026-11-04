# AER 常驻服务与 bwg06

原有 `compose.yaml` 仍是一次性 Runtime。按需启用 `compose.services.yaml`，
启动只读可视化后台和带 Bearer 认证的 MCP；两者使用同一不可变镜像与现有持久目录。
不把演示数据写入线上数据库，不把未知任务结果计为成功。

## 发布

先在 CI 构建并验证完整提交，通过现有 deploy workflow 的手动入口选择对应 ref 发布。
服务器仅拉镜像，原部署脚本先备份 SQLite、迁移、冒烟，再记录 `current.env`。
将受版本控制的 `deploy/compose.services.yaml` 同步到 `/srv/aer/deploy/`。
凭据保存在服务器 `/srv/aer/deploy/services.env`，权限 0600，不能提交或复制进浏览器配置：

- `AER_DASHBOARD_PASSWORD`：随机生成，至少 16 字符。
- `AER_DASHBOARD_SESSION_SECRET`：随机生成，至少 32 字符。
- `AER_MCP_TOKEN`：独立随机生成，至少 32 字符。

在完成镜像部署后启动：

```bash
cd /srv/aer/deploy
set -a
. ./current.env
set +a
docker compose -f compose.services.yaml up -d
docker compose -f compose.services.yaml ps
curl --fail http://127.0.0.1:8788/healthz
```

看板监听 `127.0.0.1:8788`，MCP 监听 `127.0.0.1:18765`；容器网络端口不代表公网开放。
非标准 MCP Host 被现有防 DNS rebinding 机制拒绝时，应使用隧道本地端口 8765，
不要关闭该防护。通过 SSH：

```bash
ssh -N -L 8788:127.0.0.1:8788 -L 8765:127.0.0.1:18765 bwg-06
```

浏览器打开 `http://127.0.0.1:8788`，输入服务器保管的看板密码。
授权管理员通过 SSH 在服务器读取 `services.env` 获取凭据；不要发到公共聊天。
MCP 地址为 `http://127.0.0.1:8765/mcp`，使用独立 Bearer token。

## 回退与边界

服务启动失败时停止新服务，原有 SQLite 与备份保留。选择先前兼容的不可变镜像重启；
不得自动 downgrade 数据库。原 Runtime 仍能执行备份、索引和诊断命令。
上线后核对容器镜像 SHA、健康接口、未登录拒绝、登录后数据、Run/经验数量不增加。

这是技术部实例。没有客户级隔离的 AER 不能直接充当 blogo 多客户运营经验库；
blogo 仍需要独立的、经过审核的试点运营集合。部署服务本身也不会自动同步各开发机的本地轨迹。
