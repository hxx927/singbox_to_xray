# singbox_to_xray

将 S-UI 生成的 sing-box 入站转换为 Xray 入站，并安全合并到 miaomiaowuX Agent 管理的 Xray 配置。支持直接读取新版 S-UI SQLite 数据库，不依赖另一个 sing-box 安装留下的 `config.json`。

脚本只使用 Python 标准库，默认支持预检、备份、原子写入、Xray 重启检查、Agent 上报、主控节点同步确认和一键回滚。

## 支持范围

| sing-box 入站 | Xray 入站 | 传输与安全层 |
|---|---|---|
| VLESS | `vless` | TCP、WebSocket、gRPC、HTTP、HTTPUpgrade、TLS、REALITY |
| VMess | `vmess` | TCP、WebSocket、gRPC、HTTP、HTTPUpgrade、TLS |
| Trojan | `trojan` | TCP、WebSocket、gRPC、HTTP、HTTPUpgrade、TLS |
| Shadowsocks | `shadowsocks` | 传统 AEAD、2022 多用户 |
| Hysteria2 | `hysteria` version 2 | TLS、salamander |
| SOCKS / HTTP | `socks` / `http` | 有认证、无认证 |

TUIC、ShadowTLS、Naive、mixed、tun 等没有直接等价 Xray 入站的类型默认跳过；使用 `--strict` 时会直接失败。

## 安装

一键安装到 `/usr/local/bin/singbox-to-xray`：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx927/singbox_to_xray/main/install.sh | sudo sh
```

安装器同时创建短命令 `/usr/local/bin/s-x`。root 用户直接输入 `s-x`，普通用户输入 `sudo s-x`，即可打开中文交互菜单：

```text
singbox_to_xray 0.3.x
========================================
  1. 查看数据源与可迁移入站
  2. 安全预检（推荐，不写入）
  3. 选择数据源后预检
  4. 隔离端口预检
  5. 正式迁移到 Xray
  6. 回滚最近一次迁移
  7. 显示命令帮助
  0. 退出
========================================
```

正式迁移必须手动输入 `APPLY`，回滚必须输入 `ROLLBACK`。菜单不会自动停止 S-UI，也不会绕过端口占用检查。原来的 `singbox-to-xray deploy ...` 参数式命令继续保留，适合自动化执行。

也可以直接克隆运行：

```bash
git clone https://github.com/hxx927/singbox_to_xray.git
cd singbox_to_xray
python3 singbox_to_xray.py --version
```

要求：

- Linux 与 Python 3.9+
- 已安装并由 systemd 管理的 Xray
- 默认自动检测 `/usr/local/s-ui/db/s-ui.db` 和 `/usr/local/etc/sing-box/config.json`
- 只要 S-UI 数据库中存在入站，就优先使用 S-UI 数据库
- Xray 默认配置路径为 `/usr/local/etc/xray/config.json`
- 使用 `--notify-master` 时，需要 miaomiaowuX 主控提供 `/api/remote/sync-nodes`

## 数据源选择

不指定数据源时，脚本按以下顺序自动发现：

1. `/usr/local/s-ui/db/s-ui.db` 中由 S-UI 管理的动态入站。
2. `/usr/local/etc/sing-box/config.json` 中的普通 sing-box 入站。

脚本以 SQLite 只读模式访问 S-UI 数据库，按照 S-UI 1.5.x 的规则组装 TLS 和已启用客户端，不修改数据库。每次执行都会明确打印实际选择的数据源：

```text
[SOURCE] selected S-UI database /usr/local/s-ui/db/s-ui.db (1 inbound(s))
```

多个来源都有入站时，可以交互选择：

```bash
sudo singbox-to-xray deploy --interactive --strict
```

也可以显式指定来源，跳过自动发现：

```bash
sudo singbox-to-xray deploy \
  --s-ui-db /usr/local/s-ui/db/s-ui.db \
  --strict

sudo singbox-to-xray deploy \
  --input /path/to/sing-box-config.json \
  --strict
```

新版 S-UI 把入站保存在数据库中，并由 S-UI 进程动态组装 sing-box 配置。单独检查 `/usr/local/etc/sing-box/config.json` 不能证明其中包含 S-UI 节点。

## 安全使用流程

推荐直接打开菜单并选择 `2`，自动读取 S-UI 入站并做预检：

```bash
sudo s-x
```

等价的参数式命令如下，不修改配置：

```bash
sudo singbox-to-xray deploy --strict
```

确认 S-UI Core 已停止并释放原入站端口后，执行正式迁移：

```bash
sudo singbox-to-xray deploy --strict --apply --notify-master
```

## 正式迁移提示端口占用

在菜单选择 `5` 后，如果目标端口仍由 S-UI 或 sing-box 占用，脚本会在写入 Xray 前停止，并针对实际端口和进程显示处理步骤。例如检测到新版 S-UI 时会提示：

```text
目标端口仍被旧进程占用：50965(sui)
Xray 配置尚未写入，请按下面步骤处理：
1. 保持当前 SSH 会话，另开一个 SSH 窗口连接服务器。
2. 执行：systemctl stop s-ui
3. 使用提示中的 ss 命令确认端口已释放。
4. 回到 s-x 菜单，重新选择 5。
```

停止 `s-ui` 会让面板暂时离线，但不会删除 S-UI 数据库或节点。独立 sing-box 占用时，脚本会改为提示检查并停止 `sing-box` 服务；其他进程则提示先用 `ss` 定位。不要使用 `--allow-active-port` 强行绕过同端口冲突。

脚本成功退出时，状态文件 `/var/lib/mmwx-singbox-migrate/state.json` 中应为：

```json
{
  "status": "synced"
}
```

仅出现 `reported` 代表 Agent 已上报，不代表主控节点已经入库。

## 已有同 tag 入站时复测

如果目标 Xray 已经包含转换后的同名入站，普通部署会按设计拒绝覆盖。先执行不写盘预检：

```bash
sudo singbox-to-xray deploy --strict --replace-existing
```

主控升级完成后，才能执行实际替换和同步：

```bash
sudo singbox-to-xray deploy \
  --strict --replace-existing \
  --apply --notify-master
```

需要完全隔离测试时，可以改写端口和 tag：

```bash
sudo singbox-to-xray deploy \
  --strict \
  --port-offset 10000 \
  --tag-suffix=-stage \
  --apply --notify-master
```

## 回滚

恢复最近一次部署前的 Xray 配置，并同步清理已从 Agent 消失的迁移节点：

```bash
sudo singbox-to-xray rollback --notify-master
```

成功时状态为 `rolled_back_synced`。主控只会删除属于当前 Agent 服务器、且已确认不再存在于 Xray 入站中的 tag。

## 只转换

输出完整 Xray `inbounds` 对象：

```bash
singbox-to-xray convert \
  --input /usr/local/etc/sing-box/config.json \
  --output /tmp/xray-inbounds.json
```

只迁移指定 tag：

```bash
sudo singbox-to-xray deploy --tag vless-main --strict
```

显式端口映射：

```bash
sudo singbox-to-xray deploy \
  --port-map 443=8443 \
  --port-map 2096=12096
```

## 无伤迁移约束

- 不覆盖整个 Xray 配置，只合并顶层 `inbounds`。
- 保留 `api`、`stats`、`policy`、`metrics`、路由和出站。
- 写盘前必须通过 `xray run -test -config`。
- 每次部署创建 root-only 备份和状态文件。
- 使用同目录临时文件和 `os.replace` 原子写入。
- Xray 启动失败或端口未监听时自动恢复备份。
- 默认拒绝 tag、端口碰撞及非 Xray 进程占用目标端口。
- 不在日志中输出 UUID、密码、证书私钥或 REALITY 密钥。
- `--notify-master` 只有收到主控返回的实际 `node_tags` 后才算成功。

## 主控兼容性

完整闭环要求 miaomiaowuX 主控包含以下能力：

- `scan_result` 回调异步处理，避免 WebSocket 读取循环等待同连接的 RPC reply。
- `POST /api/remote/sync-nodes`，使用 Agent 配置中的服务器 token 鉴权。
- 同步后返回当前服务器实际持久化的 `node_tags`。
- 回滚时只清理当前服务器中已从 Agent 入站消失的 tag。

旧主控返回 404 时，脚本会保留已经健康运行的 Xray 配置，但以非零状态退出并记录 `master_sync_failed`，不会谎报节点同步成功。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前测试覆盖 S-UI SQLite 动态入站组装、数据源选择、协议字段映射、TLS/REALITY、传输层、端口映射、合并冲突、Agent YAML 读取和主控节点确认。

## 文档

- [完整迁移教程](docs/migration-guide.md)
- [设计、同步链路与实机验证](docs/design.md)

## License

[MIT](LICENSE)
