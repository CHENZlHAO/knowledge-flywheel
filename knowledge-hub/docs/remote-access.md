# 远程访问（非局域网）Runbook

目标：让边缘端与中心端在**不在同一个局域网**时仍能交互。核心只有一个变量——边缘端
`--center-url` 必须能通过某种网络路径访问到中心端的 HTTP 端口。

## 方案对比

| 方案 | 难度 | 是否需公网 IP | 推荐度 | 说明 |
|---|---|---|---|---|
| Tailscale / ZeroTier（组网） | 低 | 否 | ⭐ 推荐 | 中心端与边缘端各装客户端即组成加密 overlay，得到固定内网 IP/MagicDNS，最省心 |
| 公网 IP + 端口映射 + 反向代理 TLS | 中 | 是（或运营商可映射） | 可用 | 中心端暴露到公网，前面必须加 Caddy/Nginx 做 TLS 与访问控制 |
| VPS/云主机托管中心端 | 中 | VPS 自带公网 IP | 可用 | 中心端部署到云上，边缘端走公网 |
| frp / ngrok 内网穿透 | 中 | 否（需一台有公网 IP 的 frp 服务端） | 可用 | 适合临时测试 |

> ⚠️ 当前节点侧是**信任模型**（不再校验 X-Node-Key），所以中心端端口**绝不能裸奔到公网**。
> 用 Tailscale 等 overlay 或反向代理，把中心端端口限制在 overlay/白名单内；管理员、搜索、
> 下载、移动端等仍保留各自密钥。

## 推荐方案：Tailscale

1. 中心端主机安装 Tailscale 并登录：
   ```bash
   # macOS / Linux（Windows 用安装包）
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. 边缘端主机同样安装并登录同一 tailnet（或按企业组织账号加入）。
3. 记下中心端主机的 overlay 地址：
   ```bash
   tailscale ip -4
   # 例如 100.101.102.103；开启 MagicDNS 后也可用 <主机名>.tailnet-name.ts.net
   ```
4. 中心端保持监听 0.0.0.0（默认 `API_HOST=0.0.0.0`），无需改防火墙——Tailscale 会在 overlay 网卡上提供 8000 端口访问。
5. 边缘端启动时把中心地址指向 overlay：
   ```bash
   ./knowledge-edge-agent --center-url=http://100.101.102.103:8000 --node-id=pc-001 --watch-dir=./knowledge
   ```
6. 验证：
   ```bash
   # 边缘端
   curl -s http://100.101.102.103:8000/healthz
   # 应返回 {"status":"ok",...}
   ```
   然后看中心端控制台「总览 → 节点状态」出现该节点并显示 online。

## 方案二：公网 IP + 反向代理（需要 TLS）

1. 中心端保持 `API_HOST=0.0.0.0:8000`，在路由器把公网端口（如 443）映射到中心端主机 8000。
2. 前置 Caddy 自动 HTTPS：
   ```caddyfile
   knowledge.example.com {
       reverse_proxy 127.0.0.1:8000
       # 可选：基础访问控制
       basicauth {
           ops $2a$14$...
       }
   }
   ```
3. 边缘端：
   ```bash
   ./knowledge-edge-agent --center-url=https://knowledge.example.com --node-id=pc-001 --watch-dir=./knowledge
   ```

## 方案三：中心端部署到云主机

把 Docker Compose 栈（或 `knowledge-center` 二进制）部署到 VPS，边缘端 `--center-url=http://<VPS公网IP>:8000`，
生产环境务必加 TLS 反向代理。

## 同时要注意 Dify 的可达性

中心端代理 Dify 问答/同步时，中心端进程需要能访问 Dify 的 Base URL（后台配置里那项）。
Dify 不必对边缘端开放，只要**中心端 → Dify** 通即可：
- 本机 Dify：`http://localhost`
- 同机 Docker Dify：`http://localhost`（或 compose 服务名，如 `http://dify-api`）
- 远程 Dify：填其 overlay/公网地址

## 防火墙与最小暴露清单

中心端需要开放的入站端口：

| 端口 | 用途 | 建议暴露范围 |
|---|---|---|
| 8000 | 中心 API + 控制台 | 仅 Tailscale overlay / 反向代理来源 |
| 8883 | MQTT TLS（可选） | 仅 overlay 内节点 |
| 5432/6379 等 | 仅本机服务间，绝不公网 | - |

边缘端只需**出站**到中心端，不需要任何入站端口（其网页控制台默认只监听 `127.0.0.1:9090`）。
