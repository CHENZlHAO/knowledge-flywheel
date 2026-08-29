# 当前阶段输入与输出契约

本文描述当前 MVP 已实现的输入、输出和手机端预留接口。真实向量检索已接入可选 Ollama adapter；服务不可用时明确降级。

## 1. 中心端输入

| 输入 | 入口 | 当前处理 |
|---|---|---|
| 节点心跳 | `POST /api/v1/nodes/heartbeat` | 更新节点主机名、IP、Agent 版本、CPU、磁盘和最后心跳 |
| 文件清单报告 | `POST /api/v1/files/report` | 按 `(path, file_hash)` 幂等登记文件 |
| UTF-8 文本内容 | `POST /api/v1/files/{file_id}/content` | 校验来源节点、文件哈希和内容 SHA-256，创建幂等解析任务 |
| 任务创建 | `POST /api/v1/tasks` | 按 `idempotency_key` 幂等创建任务 |
| AI/规则提案 | `POST /api/v1/proposals` | 写入待审核提案，不直接修改知识库 |
| 管理员审核 | `POST /api/v1/proposals/{id}/review` | 执行一次性批准/拒绝并写审计日志 |
| 告警查询 | `GET /api/v1/alerts` | 查询节点离线、文件缺失、任务失败等持久化告警 |
| 告警确认/关闭 | `POST /api/v1/alerts/{id}/ack` / `resolve` | 管理员确认或关闭告警并写审计日志 |
| 用户问答反馈 | `POST /api/v1/flywheel/feedback` | 追加评分/评论证据；按幂等键去重，不修改知识库；生产必须携带 `X-Flywheel-Key` |
| 检索事件 | `POST /api/v1/flywheel/retrievals` | 追加检索次数和无结果证据；按幂等键去重，不修改知识库；生产必须携带 `X-Flywheel-Key` |
| Dify 飞轮 Webhook | `POST /api/v1/integrations/dify/flywheel-events` | 将 Dify `retrieval`/`feedback` 事件统一映射到飞轮表；复用幂等、鉴权和 actor 注入规则 |
| 知识检索 | `POST /api/v1/knowledge/search` | 返回当前 alive 文件版本的相似切片，并记录检索事件 |
| 知识缺口聚合 | `GET /api/v1/flywheel/gaps` | 按归一化查询统计检索、无结果、正负反馈和确定性分数 |
| 飞轮优化草案 | `POST /api/v1/flywheel/proposals?query=...` | 将缺口封装为 `flywheel_optimization` Proposal，必须人工审核 |
| 手机远程命令 | `POST /api/v1/mobile/commands` | 生成排队中的远程命令，不直接执行 |
| 边缘命令回执 | `POST /api/v1/nodes/{node}/commands/{id}/ack` | 由 Agent 回报运行中、成功或失败 |
| 边缘领取命令 | `GET /api/v1/nodes/{node}/commands/next` | 原子领取一条命令并变为 `running` |

文件报告会同时创建一个幂等的 `file_register` 任务。Worker 校验任务中的 `file_id` 与 `file_hash` 是否仍和文件元数据一致，成功后将文件状态置为 `registered`；重复上报已登记的同一哈希不会让状态回退。

内容接口是当前阶段的受控输入适配器：只接受 UTF-8 文本，且
`sha256(content.encode("utf-8"))` 必须等于文件报告中的 `file_hash`。解析后会追加幂等 embedding 任务；Ollama 成功为 `ready/ollama`，不可用时为 `degraded/deterministic_fallback`。检索只允许文件哈希匹配的 alive 版本，同路径新哈希会将旧版本标为 `superseded`。

## 2. 中心端输出

| 输出 | 入口 | 含义 |
|---|---|---|
| 健康状态 | `GET /healthz` | API 和数据库连接可用 |
| 节点列表 | `GET /api/v1/nodes` | 节点在线/离线、CPU、磁盘和最后心跳 |
| 文件统计 | `GET /api/v1/files/summary` | 总文件数、缺失数、健康率 |
| 文件存活巡检 | `POST /api/v1/reconciliation/files` | 按 `FILE_MISSING_AFTER_SECONDS` 将长期未上报文件标为 `missing`，返回检查、缺失和恢复数量 |
| 固定副本池配置 | `REPLICA_NODE_IDS` | 逗号分隔的中心副本节点 ID；巡检按此策略生成幂等 `replica_repair` 任务 |
| 文件流水线 | `GET /api/v1/pipeline/files` | 文件状态与登记任务状态、错误、来源节点和更新时间 |
| 文件切片 | `GET /api/v1/files/{file_id}/chunks` | 按顺序返回已持久化的文本切片和切片哈希 |
| 下载网关 | `GET /api/v1/gateway/files/{file_id}` | 独立密钥保护的当前文本版本下载；返回 `ETag`、`X-File-Hash`、`X-File-Version`；缺失/旧版本拒绝 |
| 文件副本列表 | `GET /api/v1/files/{file_id}/replicas` | 返回文件在各节点的持有状态、哈希和最后上报时间 |
| 节点文件列表 | `GET /api/v1/nodes/{node_id}/files` | 返回节点持有的文件副本清单，供节点状态面板使用 |
| 副本总览 | `GET /api/v1/replicas` | 返回全网副本关系和 `healthy/missing` 状态 |
| 副本策略健康 | `GET /api/v1/replica-policy/health` | 返回合法/非法固定副本节点、期望/健康副本数、健康率及修复任务统计 |
| 副本修复任务 | `GET /api/v1/replica-repairs` | 返回修复任务的 `pending/running/waiting/success/failed`、目标节点和错误 |
| 重试副本修复 | `POST /api/v1/replica-repairs/{task_id}/retry` | 仅允许失败的副本修复重新排队，并重置失败的 Agent 命令 |
| 任务列表 | `GET /api/v1/tasks` | 任务状态、尝试次数和错误 |
| 提案列表 | `GET /api/v1/proposals` | 待审核和已审核提案 |
| 手机总览 | `GET /api/v1/mobile/overview` | 节点、文件、待执行远程命令和移动端能力 |
| 手机节点列表 | `GET /api/v1/mobile/nodes` | 适合手机列表页的节点数据 |
| 手机告警列表 | `GET /api/v1/mobile/alerts` | 只读未关闭告警，供未来手机端使用 |
| 手机飞轮缺口 | `GET /api/v1/mobile/flywheel/gaps` | 只读缺口摘要，供未来手机端使用；不能创建或批准 Proposal |
| 远程命令状态 | `GET /api/v1/mobile/commands/{id}` | `queued/running/success/failed` 状态 |

## 3. 手机端预留接口

手机端统一调用中心 API，不直接连接员工电脑。当前预留命令类型：

- `restart_agent`：重启指定节点 Agent。
- `reset_sync`：重置指定节点同步状态。
- `retry_task`：重试指定任务。

边缘适配器通过 `GET /api/v1/nodes/{node}/commands/next` 领取命令，执行完成后调用回执接口。命令传输仍使用受保护的 HTTP 领取契约；MQTT 安全桥接仅承载节点状态/遗嘱事件，不把 MQTT 作为任意数据库写入口。

命令创建成功只代表进入 `queued`，返回 `execution_mode: adapter_pending`。真正执行需要后续 MQTT/Agent 命令适配器；在适配器完成前，不得向用户显示“已执行”。

边缘 Agent 领取命令后会携带 `claimed_at` 领取时间。超过 `REMOTE_COMMAND_LEASE_SECONDS`
仍未回执的 `running` 命令会在该节点下一次领取时自动回到 `queued`，并写入审计日志，避免 Agent 崩溃造成永久卡单。

示例：

```json
{
  "node_id": "node-001",
  "command_type": "restart_agent",
  "idempotency_key": "mobile-node-001-restart-20260826-001",
  "payload": {},
  "requested_by": "mobile-user-001"
}
```

返回：

```json
{
  "id": 12,
  "status": "queued",
  "execution_mode": "adapter_pending",
  "command_type": "restart_agent",
  "node_id": "node-001"
}
```

生产环境必须设置 `MOBILE_API_KEY`，并在网关层接入 OIDC/SSO、RBAC、MFA、设备撤销和审计查询。当前开发环境允许无 Key 访问，仅用于本地验收。

手机端飞轮接口只允许读取聚合后的缺口摘要。反馈和检索事件仍由 Dify/主机前台或受信任服务写入；移动端不得直接写入事件、生成 Proposal、批准 Proposal 或修改知识库。

生产环境的飞轮写入接口由 `FLYWHEEL_INGEST_API_KEY` 保护。网关应通过 `X-Actor` 注入经过认证的用户/服务身份；请求体中的 `actor` 不作为可信身份，仅原样保存到 `metadata.client_actor` 供审计。缺少密钥或网关注入身份时请求必须被拒绝或标记为网关服务账号，避免客户端伪造缺口贡献者。

飞轮事件示例：

```json
{
  "idempotency_key": "dify-session-42-turn-7",
  "query": "如何申请年假",
  "result_count": 0,
  "actor": "user-42",
  "metadata": {"source": "dify", "conversation_id": "session-42"}
}
```

Dify 可直接发送统一事件：`event_type` 为 `retrieval` 时必须提供 `result_count`，为 `feedback` 时必须提供 `rating`。生产请求使用 `X-Flywheel-Key` 和网关注入的 `X-Actor`，成功响应返回中心事件 ID；重复发送同一 `idempotency_key` 返回同一事件。

聚合输出示例：

```json
{
  "normalized_query": "如何申请年假",
  "retrieval_count": 12,
  "no_result_count": 8,
  "negative_feedback_count": 3,
  "positive_feedback_count": 1,
  "score": 19,
  "last_seen_at": "2026-08-28T09:00:00Z"
}
```

当前聚合是确定性规则：无结果每次加 2 分，1-2 分负反馈每次加 1 分，正反馈只计数不扣分。Proposal 的 body 会携带原始统计和 `human_review_required: true`；真正写入知识库前必须走现有 `POST /api/v1/proposals/{id}/review` 审核接口。生产接入时必须由网关注入可信用户身份，禁止客户端伪造 `actor`。

边缘 Agent 生产环境应设置与中心 `NODE_API_KEY` 一致的 `--node-api-key` 参数；开发环境可留空。

文件流水线中还会输出 `alive` 与 `last_seen_at`。Worker 每个轮询周期执行一次基于清单时间的存活巡检；文件重新上报后恢复为 `alive=true`。缺失和恢复均写入审计日志。该机制证明的是“中心是否持续收到来源节点清单”，不是中心磁盘逐文件哈希读取。

## 4. 副本阶段边界

当前已实现副本元数据、持有关系、存活巡检和可审计修复调度。只有已经通过心跳注册且 `is_replica=true` 的 `REPLICA_NODE_IDS` 节点会成为修复目标；非法配置通过策略健康接口和控制台告警，不创建任务。

Worker 将 `replica_repair` 转换为目标 Agent 可领取的 `sync_replica` 命令，并把任务置为 `waiting`。Agent 限制目标为 `--watch-dir` 内相对路径，拒绝绝对路径和目录穿越。当前 Agent 只能验证已存在字节的 SHA-256：成功 ACK 必须包含 `verified=true` 和匹配 `file_hash`，中心才写入健康副本并完成任务；文件不存在时明确失败为适配器未安装。真实 Syncthing 拉取仍待下一阶段接入，系统不会把“已派发”表示成“已同步”。

## 5. 当前不在输出范围内

- 文件实际同步副本和自动修复结果（适配器待接入）。
- Dify 原生问答、Webhook/日志适配和下载网关。
- Dify 问答、下载链接和向量库结果。
- LangGraph/Celery 的真实任务执行结果。
- DeepSeek-Harness 的模型审核结果。
- 远程命令真实执行结果，除非后续 Agent 回执适配器已部署。

## 6. 生产适配器新增契约

| 输入 | 入口 | 处理 |
|---|---|---|
| 二进制内容 | `POST /api/v1/files/{file_id}/blob`（节点鉴权；`X-File-Hash` + `X-Node-Id` + 原始字节） | 校验 SHA-256 后写入对象存储并登记 `blob_objects` |
| 全量流水线运行 | `POST /api/v1/pipeline/run`（管理员） | 幂等入队 `pipeline_run` 任务，执行 register→parse→embed→replica |
| DSH 文档审核 | `POST /api/v1/dsh/review?file_id=`（管理员） | DSH 或确定性降级结果 → 待审核 `document_review` Proposal |
| 告警投递触发 | `POST /api/v1/alert-deliveries/process`（管理员） | 重试 pending/failed 投递，超限转死信 |
| API 密钥创建/轮换 | `POST /api/v1/admin/keys`（管理员） | 生成数据库密钥，明文仅显示一次 |
| API 密钥撤销 | `POST /api/v1/admin/keys/{key_id}/revoke`（管理员） | 撤销并写审计 |
| JWT 签发 | `POST /api/v1/admin/tokens`（管理员） | 为可信主体签发 HS256 令牌 |
| 一键备份 | `POST /api/v1/admin/backup`（管理员） | 数据库转储 + 对象存储归档 + 校验清单 |

| 输出 | 入口 | 含义 |
|---|---|---|
| 编排状态 | `GET /api/v1/orchestration/status` | `state_machine`/`langgraph` 及管线节点、LangGraph 可用性 |
| DSH 状态 | `GET /api/v1/dsh/status` | DSH 是否启用、地址、超时和主机白名单 |
| 二进制下载 | `GET /api/v1/gateway/files/{file_id}/binary`（下载密钥） | 返回当前版本原始字节、`ETag`、`X-File-Hash`、存储后端 |
| 二进制清单 | `GET /api/v1/blob-objects` | 已存储二进制对象列表 |
| 告警投递 | `GET /api/v1/alert-deliveries` | 各渠道投递状态、重试次数、错误和发送时间 |
| API 密钥列表 | `GET /api/v1/admin/keys` | 密钥 ID、角色、标签、状态、最近使用、过期时间（不含明文） |
| 安全状态 | `GET /api/v1/security/status` | OIDC 配置与本地 JWT 就绪状态 |
| 备份状态 | `GET /api/v1/admin/backup/status` | 备份目录、保留策略与历史运行清单 |

二进制与文本共用同一套幂等文件登记和版本血缘；同路径新哈希仍会将旧版本标为 `superseded`。对象存储后端由 `STORAGE_BACKEND` 决定（`local` 或 `s3`），下载始终经独立密钥网关，不暴露数据库或边缘路径。
