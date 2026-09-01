from datetime import datetime
from pydantic import BaseModel, Field


class NodeHeartbeat(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    hostname: str
    ip_address: str | None = None
    agent_version: str = "unknown"
    cpu_percent: float = Field(ge=0, le=100)
    disk_free_bytes: int = Field(ge=0)
    is_replica: bool = False


class NodeStatusEvent(NodeHeartbeat):
    status: str = Field(pattern="^(online|offline)$")
    reason: str | None = Field(default=None, max_length=255)


class FileReport(BaseModel):
    node_id: str
    path: str = Field(min_length=1, max_length=1024)
    file_hash: str = Field(min_length=8, max_length=128)
    size_bytes: int = Field(ge=0)
    category: str = Field(default="未分类", max_length=128)


class FileContentUpload(BaseModel):
    source_node_id: str = Field(min_length=1, max_length=64)
    file_hash: str = Field(min_length=8, max_length=128)
    content: str = Field(min_length=1)


class TaskCreate(BaseModel):
    kind: str
    idempotency_key: str
    payload: dict = Field(default_factory=dict)


class ProposalReview(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    reviewer: str = Field(min_length=1)


class ProposalCreate(BaseModel):
    kind: str
    title: str
    body: str
    created_by: str = "system"


class NodeView(NodeHeartbeat):
    status: str
    last_seen_at: datetime


class TaskView(BaseModel):
    id: int
    kind: str
    status: str
    attempts: int
    error: str | None


class MobileCommandCreate(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    command_type: str = Field(pattern="^(restart_agent|reset_sync|retry_task)$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    payload: dict = Field(default_factory=dict)
    requested_by: str = Field(min_length=1, max_length=128)


class MobileCommandAck(BaseModel):
    status: str = Field(pattern="^(running|success|failed)$")
    result: dict = Field(default_factory=dict)
    error: str | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    idempotency_key: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=128)


class RagChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)


class GapStatusUpdate(BaseModel):
    status: str = Field(pattern="^(open|added|ignored)$")
    note: str | None = Field(default=None, max_length=500)


class ConfigUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(default="", max_length=4096)


class ApiKeyCreate(BaseModel):
    role: str = Field(pattern="^(admin|node|mobile|search|flywheel|download|mqtt_bridge)$")
    label: str = Field(default="", max_length=255)
    ttl_seconds: int | None = Field(default=None, gt=0)


class TokenCreate(BaseModel):
    principal: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(min_length=1)
    ttl_seconds: int | None = Field(default=None, gt=0)


class PipelineRunRequest(BaseModel):
    file_id: int = Field(gt=0)
    file_hash: str = Field(min_length=8, max_length=128)
    content: str | None = Field(default=None, max_length=52_428_800)
    idempotency_key: str = Field(min_length=1, max_length=255)
