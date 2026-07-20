# sing-box 入站迁移脚本设计

## 目标

脚本直接读取 S-UI 1.5.x SQLite 数据库或普通 sing-box 完整配置，将其转换为 Xray inbound，并在同一台服务器上安全合并到 miaomiaowuX Agent 管理的 Xray 配置中。部署完成后，脚本显示当前正式版主控需要的“扫描远程服务 → 接受 Agent 现状”步骤；主控额外提供 Agent 同步 API 时，也可以使用服务器 token 自动同步并核验迁移 tag。

脚本不迁移 S-UI 的数据库、流量历史、套餐关系、路由、DNS 和出站。转换边界与[迁移教程](migration-guide.md)一致。

安装器额外创建 `s-x` 快捷命令。无参数运行时进入交互菜单；菜单内部调用同一组 `inspect`、`deploy` 和 `revoke-source-clients` 处理函数，因此交互模式与参数模式共享完全相同的校验、备份和失败恢复边界。

## 实际链路

```text
S-UI SQLite / sing-box config.json
           |
           v
   singbox_to_xray.py
           |
           | 备份 + 合并 + xray -test + 原子写入
           v
    Xray config.json ---- systemctl restart xray
           |
           | 默认：面板“扫描远程服务 → 接受 Agent 现状”
           | 可选：--notify-master 重启 Agent，并调用
           | POST /api/remote/sync-nodes（Agent 服务器 token）
           v
 miaomiaowuX syncInboundsToNodes 更新节点管理
```

测试服务器使用 `connection_mode: websocket`。新版 Agent 在 WebSocket 认证成功后会关闭本地 HTTP 监听，因此不能依赖 `127.0.0.1:23889/api/child/inbounds` 完成本机部署。直接操作 Agent 已发现的 Xray 主配置、重启 Xray，再重启 Agent 触发重新扫描，是当前代码能够闭环的路径。

## 命令设计

只转换，不修改系统：

```bash
python3 singbox_to_xray.py convert \
  --input /usr/local/etc/sing-box/config.json \
  --output /tmp/xray-inbounds.json
```

生成合并预览并运行 Xray 预检，不写盘：

```bash
python3 singbox_to_xray.py deploy \
  --input /usr/local/etc/sing-box/config.json \
  --xray-config /usr/local/etc/xray/config.json
```

确认后自动停止已识别的旧 Core 并部署：

```bash
python3 singbox_to_xray.py deploy \
  --input /usr/local/etc/sing-box/config.json \
  --xray-config /usr/local/etc/xray/config.json \
  --apply --stop-source-services
```

迁移后先完成主控的“扫描远程服务 → 接受 Agent 现状”，并真实验证管理员和套餐节点，再吊销原 S-UI client：

```bash
python3 singbox_to_xray.py revoke-source-clients
```

吊销只在 Agent 本机执行：部署时保存源 client 的 SHA-256 指纹，吊销前确认 inbound 仍有替代 client，备份并测试 Xray 配置后原子写盘和重启。它不调用主控接口，也不更新节点管理 UUID；旧 `0.4.0` 状态会从记录的源数据库回填指纹。

## 转换范围

| sing-box inbound | Xray inbound | 状态 |
|---|---|---|
| VLESS | `vless` | 支持 TCP、WS、gRPC、HTTP、HTTPUpgrade、TLS、REALITY |
| VMess | `vmess` | 支持 TCP、WS、gRPC、HTTP、HTTPUpgrade、TLS |
| Trojan | `trojan` | 支持 TCP、WS、gRPC、HTTP、HTTPUpgrade、TLS |
| Shadowsocks | `shadowsocks` | 支持传统 AEAD 与 2022 多用户字段 |
| Hysteria2 | `hysteria` + `version: 2` | 支持 TLS 和 salamander 密码 |
| SOCKS / HTTP | `socks` / `http` | 支持有认证和无认证配置 |
| TUIC、ShadowTLS、Naive、mixed、tun 等 | 无直接目标 | 默认跳过并报告；`--strict` 下失败 |

## REALITY 公钥

sing-box 服务端通常只有私钥，而 miaomiaowuX 生成订阅节点需要 `realitySettings.publicKey`。脚本按以下顺序取值：

1. `--reality-public-key TAG=PUBLIC_KEY` 显式参数。
2. sing-box 配置中已有的 `public_key`。
3. 调用本机 `xray x25519 -i PRIVATE_KEY` 推导。

部署 REALITY 入站时无法取得公钥会失败，避免生成服务端能启动、订阅却不能使用的半成品节点。

## 安全约束

- 不覆盖整个 Xray 配置，只修改顶层 `inbounds`，保留 `api`、`stats`、`policy`、`metrics`、路由和出站。
- 默认拒绝覆盖同 tag 入站；只有显式 `--replace-existing` 才替换。
- `inspect` 按当前 Xray 的 tag、协议和端口把来源分为可迁移、已存在和冲突；保留 S-UI 数据库不等于重复迁移。
- `--select-inbounds` 与可重复 `--tag` 共用 `ConversionOptions.selected_tags`，单选、多选和全选不会产生不同转换路径。
- 全量正式迁移检测到端口由 `sui`/`s-ui` 或 `sing-box` 占用时，可在用户明确确认后停止对应 systemd 服务；其他进程一律拒绝自动停止。
- 部分正式迁移不停止整个旧 Core；交互模式等待用户只停用所选入站，并复查所选端口已释放、未选在线端口仍在监听。
- 拒绝重复 tag、无效端口、转换后端口碰撞，以及与现有 Xray 入站的端口碰撞。
- 写盘前必须通过 `xray run -test -config`。
- 写盘采用同目录临时文件和 `os.replace`，避免进程中断留下半截 JSON。
- 每次部署创建带时间戳的只读备份，并记录到 root-only 状态文件。
- 状态文件中存在未 REVOKE、且对应 tag 仍在 Xray 的批次时拒绝新的正式部署，避免覆盖源 client 指纹。
- Xray 重启失败时自动恢复备份并再次启动 Xray；部署失败时，脚本停止过的来源服务也会自动重新启动。
- `--notify-master` 只在 Xray 已确认运行后重启 Agent，并必须收到主控的节点表确认。
- 自动同步和显式同步按 `server_id` 串行，避免 Agent 重连时并发创建重复节点。
- 同步 API 的服务器 token 只能同步自己的服务器。
- 日志只输出协议、tag 和端口，不打印 UUID、密码、证书或 REALITY 密钥。

## 临时端口测试

可用 `--port-offset` 或多个 `--port-map OLD=NEW` 生成测试入站：

```bash
python3 singbox_to_xray.py deploy \
  --input /usr/local/etc/sing-box/config.json \
  --xray-config /usr/local/etc/xray/config.json \
  --port-offset 10000 --tag-suffix=-stage \
  --apply --notify-master
```

临时入站会在主控创建临时节点。测试完成后，需要从 miaomiaowuX 和 Agent 当前 Xray 配置中删除 stage tag，确认临时端口释放，再扫描远程服务并接受 Agent 现状。正式迁移仍应使用原 tag 和原端口，并在 S-UI Core 已释放端口后执行。

## 主控同步方式

当前正式版主控使用面板工作流：在服务管理扫描远程服务，检查差异后接受 Agent 现状，再到节点管理执行 TCPing。脚本迁移成功后会打印完整步骤，并把状态写为 `manual_sync_required`。

自动同步的成立条件：

1. `mmw-agent` 已连接正确主控，且版本支持 WebSocket RPC 和 `scan_result`。
2. 主控已升级到包含 `/api/remote/sync-nodes` 和异步 `scan_result` 回调的版本。
3. Xray 在 Agent 上报时处于 running。
4. 入站包含非空唯一 tag、可识别协议、端口、认证 settings 和主控生成订阅需要的 TLS/REALITY 字段。
5. 主控中该 Agent 对应的服务器记录仍存在，且能确定节点使用的服务器 IP 或域名。

脚本以主控返回的 `node_tags` 为自动同步的最终证据。接口返回 404 时，迁移仍正常成功，状态写为 `manual_sync_required` 并显示面板操作步骤；token 失效、RPC 超时或预期 tag 未入库仍记录 `master_sync_failed`。

## 测试计划

1. 单元测试覆盖协议字段映射、TLS/REALITY、传输层、端口改写、跳过与严格模式。
2. 使用测试机真实 S-UI 配置执行 `convert` 和不写盘的 `deploy` 预检。
3. 在测试机备份配置后执行正式 `deploy --apply --notify-master`。
4. 验证 Xray running、目标端口监听、Agent 日志出现新的 `scan_result` 入站数量。
5. 验证主控响应的 `node_tags`；之后手动删除测试节点和 Xray 入站，确认 Agent、端口和节点表恢复。

## 实机验证结果

2026-07-18 在 Ubuntu 24.04、Xray 26.3.27、WebSocket 模式 mmw-agent 的测试服务器上完成：

- 读取 S-UI 实际生成的 Shadowsocks 和带认证 SOCKS 入站。
- dry-run 合并后通过服务器上的 `xray run -test -config`，原配置哈希未变。
- 正式部署后 Xray 同时监听两个原端口的 TCP/UDP，两个 TCP 端口均可从公网连通。
- 使用原 SOCKS 账号和原 Shadowsocks 密码分别完成了实际代理出网请求。
- Agent 重启后重新认证，并上报 `xray_running=true, inbounds=2`。
- 手动恢复原配置后业务入站清空、目标端口释放，Xray 和 Agent 仍为 active；随后重新部署成功。
- 用不落盘的 runtime 入站探针确认旧主控虽收到 `scan_result`，但未完成节点同步；根因是 WebSocket 读取循环内同步回调又等待同一连接的 RPC reply。
- 代码已把回调改为异步，并增加服务器 token 限定的显式同步/核验端点；Go 回归测试覆盖了读取循环不再被回调阻塞。
- 测试机正式迁移后，主控通过服务管理的“扫描远程服务 → 接受 Agent 现状”成功生成节点，节点管理中的 TCPing 返回延迟。
- 在测试机上启动当前代码构建的隔离主控和第二个 Agent，Agent 完成加密 WebSocket 认证并上报 2 个入站；自动同步在 SQLite 中创建 Shadowsocks 和 SOCKS 节点，显式 API 返回两个预期 `node_tags`。
- 用最终脚本一键部署一个仅监听本机的临时 SOCKS 入站，状态进入 `synced`、实际代理出网成功、节点表从 2 条增加到 3 条。
- 手动删除临时入站并同步主控后，Xray 配置恢复、临时端口关闭、主控节点表恢复为 2 条；原 SOCKS 和 Shadowsocks 均再次完成真实代理请求。

上述隔离主控、第二 Agent、临时数据库、测试入站和专用备份已在验收后全部清理。当前正式主控使用面板接受 Agent 现状即可完成入库；部署 Agent 同步 API 后才可启用 `--notify-master` 自动闭环。
