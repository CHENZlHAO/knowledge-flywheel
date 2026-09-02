# Windows build and install

Build on macOS or Linux:

```bash
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o dist/knowledge-edge-agent.exe .
```

The binary has no Docker/runtime dependency. Deploy it with a signed installer or a controlled ZIP package, configure a stable `--node-id`, `--center-url`, `--watch-dir`, and (in production) `--node-api-key`, then register it as a Windows service. Remote command polling is enabled by default; set `--command-poll 0` to disable it. Production packaging must add code signing, upgrade rollback, and a service account with least filesystem permissions.

## Windows service (built-in)

The agent now embeds a native Windows SCM service:

```bat
rem Build, then install/start as Administrator:
scripts\windows\build-windows.bat 1.0.0 <optional-signing-cert-sha1>
scripts\windows\install-service.bat
rem Stop/remove:
scripts\windows\uninstall-service.bat
```

The same commands exist inside the binary (`-service install|uninstall|run`), which is
what the `.bat` wrappers call. `-service run` registers with the Service Control
Manager and shuts down cleanly on stop/shutdown. `build-windows.bat` stamps the
version with `-ldflags "-X main.agentVersion=..."` and, when a cert SHA-1 is
given, signs with `signtool` and an RFC 3161 timestamp. For batch deployment,
distribute the signed EXE plus `install-service.bat` in a ZIP/`sc.exe`-based
push; sign both the EXE and the installer.

## Auto-update contract (staged, verifiable)

Release management can publish an update manifest and have the agent verify the
new build *before* the installer swaps it:

```json
{
  "version": "1.0.1",
  "url": "https://updates.example.com/knowledge-edge-agent.exe",
  "sha256": "<64 hex>",
  "min_center_version": "0.1.0",
  "notes": "security fix"
}
```

The install/update script downloads to `knowledge-edge-agent.exe.next`, checks
SHA-256 against the manifest, then atomically replaces the running image on the
next maintenance window (stop service, move, start service). Rollback keeps the
previous signed EXE as `knowledge-edge-agent.exe.prev`. This keeps the agent
lightweight: the service itself stays a single static binary with no updater
daemon.

Example:

```powershell
.\knowledge-edge-agent.exe --node-id=pc-001 --center-url=http://hub.local:8000 --watch-dir=C:\Knowledge --node-api-key=$env:NODE_API_KEY
```

> `--node-api-key` 在当前企业内网信任模型下已非必需（中心不再校验 X-Node-Key）；保留该参数仅为兼容旧配置。

固定副本节点额外添加 `--is-replica`，且中心 `.env` 的 `REPLICA_NODE_IDS` 必须包含同一个稳定节点 ID：

```powershell
.\knowledge-edge-agent.exe --node-id=replica-001 --center-url=http://hub.local:8000 --watch-dir=D:\KnowledgeReplica --node-api-key=$env:NODE_API_KEY --is-replica
```

Agent 只上报 `--watch-dir` 内的相对路径。`sync_replica` 命令会拒绝绝对路径和目录穿越；若配置了 Syncthing REST API，它会触发指定文件扫描，等待文件落盘并逐字节验证 SHA-256，超时或不匹配都回报失败。未配置适配器时，已存在文件仍可验证，缺失文件会明确失败。

生产 MQTT 使用双向 TLS 和遗嘱下线。先在中心执行 `scripts/generate-mqtt-certs.sh`，为每个节点执行 `scripts/generate-mqtt-node-cert.sh <node-id>`，再把节点用户名设为与 `--node-id` 完全一致，并启动：

```powershell
.\knowledge-edge-agent.exe --node-id=replica-001 --center-url=https://hub.local:8000 --watch-dir=D:\KnowledgeReplica --node-api-key=$env:NODE_API_KEY --is-replica --syncthing-url=http://127.0.0.1:8384 --syncthing-api-key=$env:SYNCTHING_API_KEY --syncthing-folder-id=replica-folder --mqtt-broker=tls://hub.local:8883 --mqtt-username=replica-001 --mqtt-password=$env:MQTT_PASSWORD --mqtt-ca-file=C:\Knowledge\ca.crt --mqtt-client-cert=C:\Knowledge\replica-001.crt --mqtt-client-key=C:\Knowledge\replica-001.key
```
