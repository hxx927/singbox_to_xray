# 从 S-UI / sing-box 迁移入站到 miaomiaowuX

本文适用于把 S-UI 中由 sing-box 承载的服务端入站，迁移为 miaomiaowuX 管理的 Xray 入站。重点不是让两份 JSON 长得一样，而是保留客户端实际握手所依赖的参数，让原有客户端在切换后不需要改配置。

## 新版 S-UI 的配置来源

S-UI 1.5.x 把入站、TLS 和客户端信息保存在 SQLite 数据库中，并在 S-UI 进程内动态组装 sing-box 配置。它不要求把正在运行的完整配置写入 `/usr/local/etc/sing-box/config.json`。

脚本默认检测 `/usr/local/s-ui/db/s-ui.db`。数据库中存在入站时，它优先读取该数据库；只有 S-UI 数据库没有入站时，才回退到普通 sing-box JSON。可以先执行以下 dry-run 确认日志中的 `[SOURCE]`：

安装后可以运行 `sudo s-x` 打开中文交互菜单，选择 `1` 查看入站，选择 `2` 执行推荐的安全预检。正式迁移、旧 client 吊销和回滚分别要求输入 `APPLY`、`REVOKE` 和 `ROLLBACK`，避免误操作。

```bash
sudo singbox-to-xray deploy --strict
```

预期来源日志类似：

```text
[SOURCE] selected S-UI database /usr/local/s-ui/db/s-ui.db (1 inbound(s))
```

如果同一台机器上还有独立 sing-box 配置，使用交互模式选择来源：

```bash
sudo singbox-to-xray deploy --interactive --strict
```

也可以使用 `--s-ui-db PATH` 或 `--input PATH` 强制指定来源。脚本使用 SQLite 只读连接，不修改 S-UI 数据库。

菜单选择正式迁移后，脚本会询问是否自动停止占用目标端口的旧 Core。输入 `y` 时，只会对 `sui`/`s-ui` 和 `sing-box` 执行对应的 `systemctl stop`，确认端口释放后才写入 Xray；其他进程仍会中止迁移并显示排查命令。如果 Xray 部署失败，脚本会恢复备份并重新启动由它停止的来源服务。停止 S-UI 只会让面板暂时离线，不会删除数据库或节点。

## 先说结论

sing-box 入站不能原样粘贴给 Xray。两者表达的是同一组协议参数，但字段名和层级不同：

- sing-box 使用 `type`、`listen_port`、`users`、`tls`、`transport`。
- Xray 使用 `protocol`、`port`、`settings.clients`、`streamSettings`。
- miaomiaowuX 的入站管理最终接收的是一个 Xray 原生 inbound 对象，并把它热加载、持久化，再同步成订阅节点。

这里所说的“无伤”是指保留服务器地址、端口、UUID/密码、传输方式、TLS/REALITY 参数后，原客户端链接仍然能用。下面这些数据不能跟着入站 JSON 自动迁移：

- S-UI 的流量历史、到期时间、流量限额和客户端在线记录。
- S-UI 数据库里的用户与入站关联。
- S-UI 的路由、出站、DNS 和面板设置。
- sing-box 独有而 Xray 没有等价实现的协议或选项。

如果 sing-box 与 Xray 在同一台机器上，二者不能同时监听相同的 IP 和端口。因此可以做到“先验证、可回滚、客户端配置不变”，但最终抢占原端口时仍会有数秒切换窗口，不能承诺严格的零丢包。

## 兼容性速查

| S-UI / sing-box 入站 | 迁移结论 | miaomiaowuX / Xray 目标 |
|---|---|---|
| VLESS + TCP/REALITY | 推荐，可保留原链接 | `vless` + `tcp` + `reality` |
| VLESS + TCP/TLS | 可等价迁移 | `vless` + `tcp` + `tls` |
| VLESS + WebSocket | 协议可迁移，但见 WSS 特别说明 | `vless` + `ws` |
| VLESS + gRPC | 可等价迁移 | `vless` + `grpc` |
| VMess + TCP/WS/TLS | 可等价迁移 | `vmess` |
| Trojan + TCP/WS/gRPC + TLS | 可等价迁移 | `trojan` |
| Shadowsocks AEAD | 可迁移 | `shadowsocks` |
| Shadowsocks 2022 多用户 | 可迁移，但必须正确拆分主密钥和用户密钥 | `shadowsocks` 2022 |
| Hysteria2 | 可迁移，Xray 协议名是 `hysteria` | `hysteria`，`version: 2` |
| AnyTLS | 仅 miaomiaowuX 内嵌 Xray 支持 | `anytls` |
| SOCKS / HTTP | 可转换，不建议直接暴露到公网 | `socks` / `http` |
| TUIC、ShadowTLS、Naive | 无等价的受管 Xray 入站 | 改用 VLESS、Trojan 或 Hysteria2 |
| mixed、tun、tproxy、redirect、direct | 不是可直接发布为订阅节点的同类入站 | 需要重新设计用途 |

## 迁移前必须抄下来的参数

不要只保存一条客户端分享链接。迁移服务端至少需要以下信息：

1. 公网 IP 或域名、监听地址、监听端口和协议。
2. 所有用户的名称、UUID 或密码，以及 VLESS 的 `flow`。
3. 传输类型，以及 WS path/Host、gRPC service name 等参数。
4. 普通 TLS 的 SNI、证书、私钥及其文件路径。
5. REALITY 的握手目标、SNI、私钥、公钥和全部 short ID。
6. Hysteria2 的认证密码、证书、SNI、混淆参数和带宽参数。
7. 当前防火墙、反向代理和端口转发规则。

S-UI 的 REALITY 服务端配置通常只在 sing-box inbound 中保存私钥；公钥保存在 S-UI 为客户端生成的配置里。迁移前要同时从 S-UI 的 TLS/REALITY 页面记下公钥。也可以在已安装 Xray 后用私钥重新计算：

```bash
xray x25519 -i "原 REALITY 私钥"
```

不要重新生成密钥对、UUID、密码或 short ID，否则原客户端一定需要更新。

## 字段怎么对应

| sing-box | Xray inbound | 说明 |
|---|---|---|
| `type` | `protocol` | 例如两边都为 `vless` |
| `tag` | `tag` | 必须非空，并且在这台 Xray 上唯一 |
| `listen` | `listen` | `::` 可保留双栈；只要 IPv4 可用 `0.0.0.0` |
| `listen_port` | `port` | 最终切换时保持原端口 |
| `users[].name` | `settings.clients[].email` | miaomiaowuX 依赖它做用户流量统计 |
| `users[].uuid` | `settings.clients[].id` | VLESS/VMess 原样保留 |
| `users[].password` | `settings.clients[].password` | Trojan 原样保留 |
| `users[].flow` | `settings.clients[].flow` | VLESS Vision 原样保留 |
| 无 `transport` | `streamSettings.network: "tcp"` | sing-box 的原始 TCP |
| `transport.type: "ws"` | `network: "ws"` + `wsSettings` | path 和 Host 必须一致 |
| `transport.type: "grpc"` | `network: "grpc"` + `grpcSettings` | `service_name` 变为 `serviceName` |
| `tls.enabled` | `streamSettings.security: "tls"` | REALITY 除外 |
| `tls.server_name` | `tlsSettings.serverName` | 普通 TLS 的 SNI |
| `certificate_path` | `certificates[].certificateFile` | 文件必须存在且 Xray 可读 |
| `key_path` | `certificates[].keyFile` | 同上 |
| `reality.handshake.server` + `server_port` | `realitySettings.dest` | 拼成 `域名:端口` |
| `reality.private_key` | `realitySettings.privateKey` | 原样保留 |
| `reality.short_id` | `realitySettings.shortIds` | 数组原样保留 |
| REALITY 客户端公钥 | `realitySettings.publicKey` | miaomiaowuX 生成订阅节点时需要 |

Xray 新文档把 REALITY 服务端的 `dest` 推荐写成 `target`，但当前 miaomiaowuX 的生成器和辅助逻辑仍使用 `dest`，而 Xray 兼容这个旧字段名，因此迁移配置建议保留 `dest`。

## 完整示例：VLESS + TCP + REALITY

假设 S-UI 对应的 sing-box 入站如下。示例中的密钥和 UUID 都是占位符：

```json
{
  "type": "vless",
  "tag": "sui-vless-reality",
  "listen": "::",
  "listen_port": 443,
  "users": [
    {
      "name": "alice",
      "uuid": "11111111-2222-3333-4444-555555555555",
      "flow": "xtls-rprx-vision"
    }
  ],
  "tls": {
    "enabled": true,
    "server_name": "www.example.com",
    "reality": {
      "enabled": true,
      "handshake": {
        "server": "www.example.com",
        "server_port": 443
      },
      "private_key": "SUI_REALITY_PRIVATE_KEY",
      "short_id": [
        "6ba85179e30d4fc2"
      ]
    }
  }
}
```

转换成下面这个 Xray inbound。第一次并行测试时先使用未占用的 `24443`；正式切换时再改回原来的 `443`。

```json
{
  "tag": "sui-vless-reality",
  "listen": "::",
  "port": 24443,
  "protocol": "vless",
  "settings": {
    "decryption": "none",
    "clients": [
      {
        "id": "11111111-2222-3333-4444-555555555555",
        "level": 0,
        "email": "alice",
        "flow": "xtls-rprx-vision"
      }
    ]
  },
  "streamSettings": {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
      "show": false,
      "dest": "www.example.com:443",
      "xver": 0,
      "serverNames": [
        "www.example.com"
      ],
      "privateKey": "SUI_REALITY_PRIVATE_KEY",
      "publicKey": "SUI_REALITY_PUBLIC_KEY",
      "shortIds": [
        "6ba85179e30d4fc2"
      ]
    }
  },
  "sniffing": {
    "enabled": true,
    "destOverride": [
      "http",
      "tls",
      "quic"
    ]
  }
}
```

这里有三个不能省略的 miaomiaowuX 细节：

- `email` 用于 Xray 用户统计。若 `alice` 也是 miaomiaowuX 用户名，流量可以归因到该用户；也可以把它设为该用户在 miaomiaowuX 中登记的邮箱。
- Xray 服务端握手只依赖 REALITY 私钥，但 miaomiaowuX 把入站转换成 Clash/订阅节点时会读取 `publicKey`，所以迁移时应同时写入。
- `tag` 是 miaomiaowuX 关联节点、套餐凭据和流量的主键之一，后续不要随意修改。

## 其他常用协议的转换片段

### VLESS / VMess + WebSocket + TLS

协议层的用户写法分别为：

```json
{
  "protocol": "vless",
  "settings": {
    "decryption": "none",
    "clients": [
      {
        "id": "原 UUID",
        "email": "原用户名",
        "level": 0
      }
    ]
  }
}
```

```json
{
  "protocol": "vmess",
  "settings": {
    "clients": [
      {
        "id": "原 UUID",
        "email": "原用户名",
        "level": 0
      }
    ]
  }
}
```

两者的 WS/TLS 传输层都可按下面填写：

```json
{
  "streamSettings": {
    "network": "ws",
    "security": "tls",
    "tlsSettings": {
      "serverName": "node.example.com",
      "alpn": [
        "http/1.1"
      ],
      "certificates": [
        {
          "certificateFile": "/path/on/xray-server/fullchain.pem",
          "keyFile": "/path/on/xray-server/privkey.pem"
        }
      ]
    },
    "wsSettings": {
      "path": "/原来的-path",
      "headers": {
        "Host": "node.example.com"
      }
    }
  }
}
```

注意：通过 miaomiaowuX 的“添加入站”接口新建 VLESS+WS 时，后端会把它改成 Nginx 托管模式：强制监听 `127.0.0.1`、分配本地端口、把 TLS 交给 Nginx，并随机生成 WS path。这适合新建节点，但不适合要求原客户端参数完全不变的迁移。

要原样保留 VLESS+WS 的端口和 path，请走下文的“完整 Xray 配置”方式追加 inbound，然后重启 Xray 并手动同步入站。VMess+WS 不触发这项 VLESS 专用改写。

### gRPC

把 sing-box 的 `transport.service_name` 改成 Xray 的驼峰字段：

```json
{
  "streamSettings": {
    "network": "grpc",
    "security": "tls",
    "tlsSettings": {
      "serverName": "node.example.com",
      "certificates": [
        {
          "certificateFile": "/path/on/xray-server/fullchain.pem",
          "keyFile": "/path/on/xray-server/privkey.pem"
        }
      ]
    },
    "grpcSettings": {
      "serviceName": "原 service_name"
    }
  }
}
```

### Trojan

```json
{
  "tag": "sui-trojan",
  "listen": "0.0.0.0",
  "port": 24443,
  "protocol": "trojan",
  "settings": {
    "clients": [
      {
        "password": "原密码",
        "email": "原用户名",
        "level": 0
      }
    ]
  },
  "streamSettings": {
    "network": "tcp",
    "security": "tls",
    "tlsSettings": {
      "serverName": "node.example.com",
      "certificates": [
        {
          "certificateFile": "/path/on/xray-server/fullchain.pem",
          "keyFile": "/path/on/xray-server/privkey.pem"
        }
      ]
    }
  }
}
```

### Hysteria2

在 miaomiaowuX/Xray 中，Hysteria2 的 `protocol` 不是 `hysteria2`，而是 `hysteria`，并在协议和传输设置中指定 `version: 2`：

```json
{
  "tag": "sui-hy2",
  "listen": "0.0.0.0",
  "port": 24443,
  "protocol": "hysteria",
  "settings": {
    "version": 2,
    "clients": [
      {
        "auth": "原 Hysteria2 password",
        "email": "原用户名",
        "level": 0
      }
    ]
  },
  "streamSettings": {
    "network": "hysteria",
    "security": "tls",
    "tlsSettings": {
      "serverName": "node.example.com",
      "alpn": [
        "h3"
      ],
      "certificates": [
        {
          "certificateFile": "/path/on/xray-server/fullchain.pem",
          "keyFile": "/path/on/xray-server/privkey.pem"
        }
      ]
    },
    "hysteriaSettings": {
      "version": 2
    }
  }
}
```

当前 miaomiaowuX 的生成器不会把 sing-box Hysteria2 的 salamander 混淆自动转换到 Xray 配置。原节点启用了 `obfs` 时，不应直接宣称“无伤”，必须使用实际客户端做兼容测试。

## 放进 miaomiaowuX 的正确方式

### 方式一：用入站向导

适合 VLESS TCP/REALITY、VLESS TCP/TLS、Trojan、VMess、Shadowsocks 和 Hysteria2 等向导已覆盖的组合。

1. 在 miaomiaowuX 添加这台服务器并安装 Agent，确认服务器已连接。
2. 首次迁移建议关闭该服务器的 443 隧道/偷自己模式，避免 miaomiaowuX 把直连端口解释成 Nginx 隧道端口。
3. 打开该服务器的入站管理，按上面的字段映射填写。
4. 测试阶段使用一个临时空闲端口，并保留全部原凭据。
5. 添加成功后检查生成的节点详情，特别是 UUID/密码、SNI、公钥、short ID、flow、path/service name。

VLESS+WS 要保留旧 path 时不要使用这种方式，原因见上面的自动改写说明。

### 方式二：编辑完整 Xray 配置

这是精确迁移和保留 VLESS+WS 参数时更可靠的方式。

1. 打开服务器的 Xray 配置编辑器，先下载或复制当前完整配置作为备份。
2. 只把转换后的单个 inbound 对象追加到现有 `inbounds` 数组。
3. 不要把 sing-box 整份配置覆盖到这里。
4. 不要删除 miaomiaowuX 默认配置中的 `api`、`stats`、`policy`、`metrics`、`tag: "api"` 的入站以及指向 `api` 出站的路由规则。
5. 先执行配置预检；通过后保存配置并从服务管理重启 Xray。
6. 回到入站/节点管理，执行“同步入站到节点”。完整配置方式不会经过“添加入站”事件，必须手动同步一次，节点才会进入 miaomiaowuX 的节点库。

不要把上面示例的整个对象再包一层 `inbounds` 后粘到入站向导。向导要的是单个 inbound；完整配置编辑器才操作顶层 `inbounds` 数组。

## 推荐的低风险切换流程

以下流程适用于 S-UI 和 miaomiaowuX/Xray 在同一台服务器上，且希望原客户端最终完全不改配置。

1. 备份 S-UI 数据库、Xray 完整配置和证书文件。
2. 保持 S-UI/sing-box 继续监听原端口。
3. 在 Xray 中用临时端口添加转换后的入站，例如把原 `443` 暂时改成 `24443`，其他参数完全不动。
4. 防火墙临时放行测试端口。复制一份客户端配置，只把端口改成 `24443`，验证 TCP/UDP、DNS、网页访问和大文件传输。
5. 确认 miaomiaowuX 能看到入站、节点和 Xray 流量统计。
6. 选择低流量时段，停止 S-UI 的 sing-box Core，并用 `ss -lntup` / `ss -lnup` 确认原端口已经释放。
7. 在完整 Xray 配置中把临时端口改回原端口，执行配置预检并重启 Xray。
8. 立即用完全未修改的旧客户端测试。此时服务器 IP/域名、端口、UUID/密码和握手参数均未变化，旧链接应直接恢复。
9. 保留 S-UI 数据和配置一段观察期，不要立刻卸载。

正式改端口时不要在入站向导里反复用同一个 tag 执行“添加”。迁移场景直接修改完整配置更清晰，也能避免配置数组里意外出现重复 tag。

如果迁移到另一台服务器，而客户端连接的是旧 IP，保留相同协议参数仍不足以让旧链接自动找到新服务器。需要额外迁移原 IP、调整 DNS，或者在旧服务器做端口转发。DNS 切换还要考虑 TTL 和仍缓存旧地址的客户端。

## 用户与套餐怎么处理

保留 Xray `clients` 后，原 S-UI 客户端凭据可以继续连接，但它们不会自动变成 miaomiaowuX 的套餐关系。

- `client.email` 与 miaomiaowuX 用户名一致，或等于该用户登记邮箱时，流量归因更容易直接命中。
- miaomiaowuX 给套餐用户下发新凭据时，通常会使用 `用户名__入站tag` 形式的 email，并把凭据记录到自己的数据库。
- 给已有用户重新绑定套餐可能会新增一个 miaomiaowuX 管理的凭据；不要在切换当天删除原 S-UI 凭据。
- 等用户已通过 miaomiaowuX 订阅拿到新凭据，并完成一个观察周期后，再逐步清理旧 client。
- 不要在迁移时批量改 `email`。它不影响协议认证，却会造成流量统计口径突然变化。

## 验收清单

- Xray 配置预检通过，服务重启后保持 running。
- 原端口只被 Xray 占用，没有 sing-box/Nginx 意外抢占。
- 原客户端不修改配置即可连接。
- VLESS Reality 的 UUID、flow、SNI、公私钥和 short ID 全部一致。
- WS 的 path、Host、TLS 终止位置一致；gRPC 的 service name 一致。
- TLS 证书链有效，Xray 进程对证书和私钥有读取权限。
- Hysteria2 同时验证 UDP 和大流量，不只做 TCP 端口探测。
- miaomiaowuX 已同步出节点，订阅中的 server/port 与客户端实际入口一致。
- Xray 的用户流量统计能看到预期 email。

## 回滚

切换后如果旧客户端无法连接：

1. 停止或移除刚迁移的 Xray 入站，释放原端口。
2. 恢复备份的 Xray 配置，避免残留重复 tag 或端口占用。
3. 启动 S-UI/sing-box Core。
4. 确认原端口重新监听，再用旧客户端验证。
5. 对照握手参数查差异，不要直接重新生成 UUID、密码或 REALITY 密钥。

## 使用一键转换脚本

仓库已提供 [`singbox_to_xray.py`](../singbox_to_xray.py)，可自动执行本文的字段转换、完整 Xray 配置合并、预检、来源服务停止、备份、重启、Agent 上报和回滚。

> 当前 miaomiaowuX 正式版本需要在服务管理中点击“扫描远程服务”，再点击“接受 Agent 现状”。脚本迁移成功后会显示这组步骤。`--notify-master` 仅用于已提供 `/api/remote/sync-nodes` 的主控；接口不存在时脚本会正常结束并记录 `manual_sync_required`。

先做不写盘预检：

```bash
python3 singbox_to_xray.py deploy \
  --input /usr/local/etc/sing-box/config.json \
  --xray-config /usr/local/etc/xray/config.json
```

让脚本停止已识别的旧 Core 并正式部署：

```bash
python3 singbox_to_xray.py deploy \
  --input /usr/local/etc/sing-box/config.json \
  --xray-config /usr/local/etc/xray/config.json \
  --apply --stop-source-services
```

回滚最近一次部署：

```bash
python3 singbox_to_xray.py rollback --notify-master
```

脚本的默认路径已匹配 S-UI 和 miaomiaowuX Agent 的常规安装，所以同机正式迁移的一键命令就是：

```bash
sudo python3 singbox_to_xray.py deploy --apply --stop-source-services
```

迁移后按脚本提示进入服务管理扫描并接受 Agent 现状；节点出现在节点管理且 TCPing 有延迟后，链路才算完整。面板确认前状态文件中的 `status` 为 `manual_sync_required`；只有自动同步接口返回实际 `node_tags` 时才是 `synced`。

确认管理员节点和用户套餐节点都能真实连接后，再删除迁移时保留的原 S-UI client：

```bash
sudo singbox-to-xray revoke-source-clients
```

也可以在 `sudo s-x` 中选择 `6` 并输入 `REVOKE`。该步骤只修改 Agent 机器当前 Xray 的 `settings.clients`/`settings.accounts`，不修改节点管理中的基础 UUID、节点 ID、套餐绑定或主控数据。脚本只删除迁移时记录的源 client 指纹，先备份并运行 `xray -test`；没有检测到替代 client 时会拒绝写盘。旧 `0.4.0` 生成的状态文件也会从原 S-UI 数据库回填指纹。

完整安全约束、协议覆盖和 Agent/主控链路见 [脚本设计文档](design.md)。

## 参考资料

- [S-UI 项目](https://github.com/alireza0/s-ui)
- [sing-box VLESS 入站](https://sing-box.sagernet.org/configuration/inbound/vless/)
- [sing-box TLS/REALITY](https://sing-box.sagernet.org/configuration/shared/tls/)
- [Xray 入站对象](https://xtls.github.io/config/inbound.html)
- [Xray VLESS 入站](https://xtls.github.io/config/inbounds/vless.html)
- [Xray REALITY](https://xtls.github.io/config/transports/reality.html)
- [Xray Hysteria2 协议](https://xtls.github.io/config/inbounds/hysteria.html)
- [Xray Hysteria2 传输](https://xtls.github.io/config/transports/hysteria.html)
