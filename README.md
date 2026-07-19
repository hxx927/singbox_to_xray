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
singbox_to_xray 0.4.2
========================================
  1. 查看数据源与可迁移入站
  2. 安全预检（推荐，不写入）
  3. 选择数据源后预检
  4. 隔离端口预检
  5. 正式迁移到 Xray（可自动停止旧 Core）
  6. 删除原 S-UI client（需先确认新节点正常）
  7. 回滚最近一次迁移
  8. 显示命令帮助
  0. 退出
========================================
```

正式迁移必须手动输入 `APPLY`，删除旧 client 必须输入 `REVOKE`，回滚必须输入 `ROLLBACK`。正式迁移还会单独询问是否自动停止占用目标端口的 `s-ui`/`sing-box`；脚本不会停止无法识别的进程，也不会绕过端口占用检查。原来的 `singbox-to-xray deploy ...` 参数式命令继续保留，适合自动化执行。

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
- `--notify-master` 是可选的；当前主控没有 Agent 同步接口时，脚本会改为显示面板手动同步步骤

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

参数模式可以让脚本停止识别出的旧 Core，并执行正式迁移：

```bash
sudo singbox-to-xray deploy --strict --apply --stop-source-services
```

## 自动处理端口占用

菜单选择 `5` 后会询问是否允许脚本自动停止来源服务。输入 `y` 后，脚本仅在端口所有者明确是 `sui`、`s-ui` 或 `sing-box` 时执行对应的 `systemctl stop`，确认端口已经释放后才写入 Xray。参数模式使用：

```bash
sudo singbox-to-xray deploy --strict --apply --stop-source-services
```

如果没有授权自动停止，脚本仍会在写入 Xray 前退出并显示手动处理步骤：

```text
目标端口仍被旧进程占用：50965(sui)
Xray 配置尚未写入，请按下面步骤处理：
1. 保持当前 SSH 会话，另开一个 SSH 窗口连接服务器。
2. 执行：systemctl stop s-ui
3. 使用提示中的 ss 命令确认端口已释放。
4. 回到 s-x 菜单，重新选择 5。
```

停止 `s-ui` 会让面板暂时离线，但不会删除 S-UI 数据库或节点。脚本不会自动停止 Nginx 等无法确认用途的进程。如果停止来源服务后 Xray 部署失败，脚本会恢复 Xray 备份，并重新启动刚才由它停止的来源服务。不要使用 `--allow-active-port` 强行绕过同端口冲突。

## 迁移完成后的主控操作

当前 miaomiaowuX 正式版本需要在面板中确认 Agent 现状。迁移成功后脚本会直接显示：

1. 打开“服务管理”，找到当前 Agent 服务器。
2. 点击“扫描远程服务”。
3. 点击“接受 Agent 现状”。
4. 到“节点管理”确认节点出现，并使用 TCPing 检查延迟。

在完成面板操作前，状态文件 `/var/lib/mmwx-singbox-migrate/state.json` 中为：

```json
{
  "status": "manual_sync_required"
}
```

支持 `/api/remote/sync-nodes` 的主控可以配合 `--notify-master` 自动确认，此时状态才会变为 `synced`。主控返回 404 不再导致整个迁移失败，脚本会记录 `manual_sync_required` 并显示上述操作步骤。

## 删除原 S-UI client

迁移完成后，脚本会在状态文件中只保存原 client 的 SHA-256 指纹，不保存 UUID 或密码明文。完成“扫描远程服务 → 接受 Agent 现状”后，确认 Xray 管理员节点和用户套餐节点都能真实连接，再执行：

```bash
sudo singbox-to-xray revoke-source-clients
```

或在 `sudo s-x` 中选择 `6` 并输入 `REVOKE`。实际删除前，脚本会按 inbound 显示 client 总数，以及原 S-UI/sing-box、miaomiaowuX 套餐用户、管理员/其他 client 的数量和标签；不会显示 UUID 或密码。套餐用户按 `用户名__入站tag` 标签识别，无法按该规则识别的 client 会归入“管理员/其他”。脚本还会确认当前 inbound 仍有至少一个替代 client，创建吊销前备份，通过 `xray -test` 后才原子写入并重启 Xray。它只删除迁移时记录的原 S-UI client，不修改 miaomiaowuX 节点管理、套餐绑定或主控数据。

REVOKE 成功后，脚本会按迁移 tag 输出一条 `jq` 命令，用于在服务器本地列出剩余 client 的标签和凭据。找到管理员 client（例如 `黑西西`）后，在“节点管理”打开同 tag 节点的 Clash 配置详情：VLESS/VMess 只替换 `uuid`，Trojan/Hysteria 只替换 `password`，其他字段不变。不要删除 Xray 入站，也不要删除节点后重新扫描。保存后更新管理员订阅并做真实连接测试；套餐用户节点无需修改。

## 已有同 tag 入站时复测

如果目标 Xray 已经包含转换后的同名入站，普通部署会按设计拒绝覆盖。先执行不写盘预检：

```bash
sudo singbox-to-xray deploy --strict --replace-existing
```

确认同名入站确实应被替换后执行：

```bash
sudo singbox-to-xray deploy \
  --strict --replace-existing \
  --apply --stop-source-services
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

恢复最近一次部署前的 Xray 配置；如果正式迁移曾由脚本停止来源服务，回滚会同时重新启动该服务：

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
- 经确认后可自动停止 `s-ui`/`sing-box`；部署失败或显式回滚时自动重新启动来源服务。
- 默认拒绝 tag、端口碰撞及非 Xray 进程占用目标端口。
- 不在日志中输出 UUID、密码、证书私钥或 REALITY 密钥。
- `--notify-master` 只有收到主控返回的实际 `node_tags` 后才算成功。

## 主控兼容性

`--notify-master` 自动闭环要求 miaomiaowuX 主控包含以下能力：

- `scan_result` 回调异步处理，避免 WebSocket 读取循环等待同连接的 RPC reply。
- `POST /api/remote/sync-nodes`，使用 Agent 配置中的服务器 token 鉴权。
- 同步后返回当前服务器实际持久化的 `node_tags`。
- 回滚时只清理当前服务器中已从 Agent 入站消失的 tag。

当前正式版本只有管理员界面/MCP 使用的 `/api/admin/remote/sync-nodes` 时，脚本无法使用 Agent token 调用它。此时 404 会被视为“Xray 迁移成功、需要手动同步”：脚本正常退出、记录 `manual_sync_required`，并提示到服务管理扫描远程服务和接受 Agent 现状。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前测试覆盖 S-UI SQLite 动态入站组装、数据源选择、协议字段映射、TLS/REALITY、传输层、端口映射、合并冲突、来源服务停止与失败恢复、Agent YAML 读取和主控节点确认。

## 文档

- [完整迁移教程](docs/migration-guide.md)
- [设计、同步链路与实机验证](docs/design.md)

## License

[MIT](LICENSE)
