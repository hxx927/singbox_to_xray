# sing-box 入站迁移脚本设计

## 目标

脚本直接读取 S-UI 1.5.x SQLite 数据库或普通 sing-box 完整配置，将其转换为 Xray inbound，并在同一台服务器上安全合并到 miaomiaowuX Agent 管理的 Xray 配置中。部署完成后，脚本等待 Agent 的 `scan_result`，再使用该服务器自身的 Agent token 请求主控同步节点，并校验节点表中已存在所有迁移 tag。

脚本不迁移 S-UI 的数据库、流量历史、套餐关系、路由、DNS 和出站。转换边界与[迁移教程](migration-guide.md)一致。

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
           | systemctl restart mmw-agent
           v
 mmw-agent 重连主控并发送 scan_result
           |
           | POST /api/remote/sync-nodes（Agent 配置中的服务器 token）
           | 主控通过 WebSocket RPC 拉取 inbounds/config
           v
 miaomiaowuX syncInboundsToNodes 更新节点
           |
           v
 返回 node_tags，脚本核对迁移 tag 后才成功退出
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

确认后部署并通知主控：

```bash
python3 singbox_to_xray.py deploy \
  --input /usr/local/etc/sing-box/config.json \
  --xray-config /usr/local/etc/xray/config.json \
  --apply --notify-master
```

回滚最近一次部署：

```bash
python3 singbox_to_xray.py rollback --notify-master
```

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
- 拒绝重复 tag、无效端口、转换后端口碰撞，以及与现有 Xray 入站的端口碰撞。
- 写盘前必须通过 `xray run -test -config`。
- 写盘采用同目录临时文件和 `os.replace`，避免进程中断留下半截 JSON。
- 每次部署创建带时间戳的只读备份，并记录到 root-only 状态文件。
- Xray 重启失败时自动恢复备份并再次启动 Xray。
- `--notify-master` 只在 Xray 已确认运行后重启 Agent，并必须收到主控的节点表确认。
- 自动同步和显式同步按 `server_id` 串行，避免 Agent 重连时并发创建重复节点。
- 同步 API 的服务器 token 只能同步自己的服务器；回滚清理前还会向 Agent 确认 tag 已不存在。
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

临时入站会在主控创建临时节点。使用 `rollback --notify-master` 时，主控会先向 Agent 确认 stage tag 已从入站消失，然后删除该服务器下对应的节点。正式迁移仍应使用原 tag 和原端口，并在 S-UI Core 已释放端口后执行。

## 主控更新的成立条件

1. `mmw-agent` 已连接正确主控，且版本支持 WebSocket RPC 和 `scan_result`。
2. 主控已升级到包含 `/api/remote/sync-nodes` 和异步 `scan_result` 回调的版本。
3. Xray 在 Agent 上报时处于 running。
4. 入站包含非空唯一 tag、可识别协议、端口、认证 settings 和主控生成订阅需要的 TLS/REALITY 字段。
5. 主控中该 Agent 对应的服务器记录仍存在，且能确定节点使用的服务器 IP 或域名。

脚本以主控返回的 `node_tags` 为最终证据。旧主控返回 404、token 失效、RPC 超时或任一预期 tag 未入库时，脚本以非零状态退出，并在状态文件记录 `master_sync_failed`。

## 测试计划

1. 单元测试覆盖协议字段映射、TLS/REALITY、传输层、端口改写、跳过与严格模式。
2. 使用测试机真实 S-UI 配置执行 `convert` 和不写盘的 `deploy` 预检。
3. 在测试机备份配置后执行正式 `deploy --apply --notify-master`。
4. 验证 Xray running、目标端口监听、Agent 日志出现新的 `scan_result` 入站数量。
5. 验证主控响应的 `node_tags`；之后执行 rollback，确认 Xray、Agent、原配置和节点表恢复。

## 实机验证结果

2026-07-18 在 Ubuntu 24.04、Xray 26.3.27、WebSocket 模式 mmw-agent 的测试服务器上完成：

- 读取 S-UI 实际生成的 Shadowsocks 和带认证 SOCKS 入站。
- dry-run 合并后通过服务器上的 `xray run -test -config`，原配置哈希未变。
- 正式部署后 Xray 同时监听两个原端口的 TCP/UDP，两个 TCP 端口均可从公网连通。
- 使用原 SOCKS 账号和原 Shadowsocks 密码分别完成了实际代理出网请求。
- Agent 重启后重新认证，并上报 `xray_running=true, inbounds=2`。
- 执行 `rollback --notify-master` 后恢复到 0 个业务入站、目标端口释放，Xray 和 Agent 仍为 active；随后重新部署成功。
- 用不落盘的 runtime 入站探针确认旧主控虽收到 `scan_result`，但未完成节点同步；根因是 WebSocket 读取循环内同步回调又等待同一连接的 RPC reply。
- 代码已把回调改为异步，并增加服务器 token 限定的显式同步/核验端点；Go 回归测试覆盖了读取循环不再被回调阻塞。
- 把最终脚本上传到测试机后单独调用同步 API，旧主控被准确识别为不支持该端点，脚本返回升级提示且未修改 Xray。
- 在测试机上启动当前代码构建的隔离主控和第二个 Agent，Agent 完成加密 WebSocket 认证并上报 2 个入站；自动同步在 SQLite 中创建 Shadowsocks 和 SOCKS 节点，显式 API 返回两个预期 `node_tags`。
- 用最终脚本一键部署一个仅监听本机的临时 SOCKS 入站，状态进入 `synced`、实际代理出网成功、节点表从 2 条增加到 3 条。
- 执行 `rollback --notify-master` 后状态进入 `rolled_back_synced`，Xray 配置哈希与备份一致，临时端口关闭，主控节点表恢复为 2 条；原 SOCKS 和 Shadowsocks 均再次完成真实代理请求。

上述隔离主控、第二 Agent、临时数据库、测试入站和专用备份已在验收后全部清理，正式 Xray/Agent 服务和原入站端口保持正常。现网主控仍需升级到本次代码后，才能直接使用新的一键闭环。
