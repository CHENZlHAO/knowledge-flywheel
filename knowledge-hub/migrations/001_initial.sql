CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS nodes (
  id VARCHAR(64) PRIMARY KEY, hostname VARCHAR(255) NOT NULL, ip_address VARCHAR(64),
  status VARCHAR(16) NOT NULL DEFAULT 'offline', agent_version VARCHAR(64) NOT NULL DEFAULT 'unknown',
  cpu_percent DOUBLE PRECISION NOT NULL DEFAULT 0, disk_free_bytes BIGINT NOT NULL DEFAULT 0,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), is_replica BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS files (
  id BIGSERIAL PRIMARY KEY, path VARCHAR(1024) NOT NULL, file_hash VARCHAR(128) NOT NULL,
  size_bytes BIGINT NOT NULL DEFAULT 0, status VARCHAR(24) NOT NULL DEFAULT 'reported', alive BOOLEAN NOT NULL DEFAULT true,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), version INTEGER NOT NULL DEFAULT 1,
  source_node_id VARCHAR(64) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_file_path_hash UNIQUE(path, file_hash)
);
ALTER TABLE files ADD COLUMN IF NOT EXISTS alive BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE files ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS ix_files_status ON files(status);
CREATE TABLE IF NOT EXISTS file_replicas (
  id BIGSERIAL PRIMARY KEY,
  file_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  node_id VARCHAR(64) NOT NULL REFERENCES nodes(id),
  status VARCHAR(24) NOT NULL DEFAULT 'healthy',
  file_hash VARCHAR(128) NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_file_replica_file_node UNIQUE(file_id, node_id)
);
CREATE INDEX IF NOT EXISTS ix_file_replicas_status ON file_replicas(status);
CREATE TABLE IF NOT EXISTS document_chunks (
  id BIGSERIAL PRIMARY KEY,
  file_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  content_hash VARCHAR(64) NOT NULL, file_hash VARCHAR(128) NOT NULL DEFAULT '',
  embedding vector(1024), embedding_provider VARCHAR(32), embedding_status VARCHAR(32) NOT NULL DEFAULT 'pending', embedded_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_document_chunk_position UNIQUE(file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS ix_document_chunks_file_id ON document_chunks(file_id);
CREATE INDEX IF NOT EXISTS ix_document_chunks_file_hash ON document_chunks(file_hash);
CREATE TABLE IF NOT EXISTS tasks (
  id BIGSERIAL PRIMARY KEY, idempotency_key VARCHAR(255) UNIQUE NOT NULL, kind VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending', payload_json JSONB NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, result_json JSONB, claimed_at TIMESTAMPTZ, claimed_by VARCHAR(128),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS result_json JSONB;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(128);
CREATE TABLE IF NOT EXISTS proposals (
  id BIGSERIAL PRIMARY KEY, kind VARCHAR(64) NOT NULL, title VARCHAR(255) NOT NULL, body TEXT NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending', created_by VARCHAR(128) NOT NULL DEFAULT 'system', reviewed_by VARCHAR(128), reviewed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGSERIAL PRIMARY KEY, actor VARCHAR(128) NOT NULL, action VARCHAR(128) NOT NULL, resource_type VARCHAR(64) NOT NULL,
  resource_id VARCHAR(128) NOT NULL, detail JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS alerts (
  id BIGSERIAL PRIMARY KEY, fingerprint VARCHAR(255) UNIQUE NOT NULL,
  severity VARCHAR(16) NOT NULL DEFAULT 'warning', kind VARCHAR(64) NOT NULL,
  resource_type VARCHAR(64) NOT NULL, resource_id VARCHAR(128) NOT NULL,
  message TEXT NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'open',
  acknowledged_by VARCHAR(128), acknowledged_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts(status);
CREATE TABLE IF NOT EXISTS flywheel_events (
  id BIGSERIAL PRIMARY KEY, idempotency_key VARCHAR(255) UNIQUE NOT NULL,
  event_type VARCHAR(32) NOT NULL, query TEXT NOT NULL, normalized_query TEXT NOT NULL,
  rating INTEGER, result_count INTEGER, comment TEXT, actor VARCHAR(128) NOT NULL DEFAULT 'anonymous',
  metadata_json JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_flywheel_events_type ON flywheel_events(event_type);
CREATE INDEX IF NOT EXISTS ix_flywheel_events_query ON flywheel_events(normalized_query);
CREATE TABLE IF NOT EXISTS remote_commands (
  id BIGSERIAL PRIMARY KEY, idempotency_key VARCHAR(255) UNIQUE NOT NULL, node_id VARCHAR(64) NOT NULL REFERENCES nodes(id),
  command_type VARCHAR(64) NOT NULL, status VARCHAR(24) NOT NULL DEFAULT 'queued', payload_json JSONB NOT NULL DEFAULT '{}',
  requested_by VARCHAR(128) NOT NULL, result_json JSONB, error TEXT,
  claimed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_remote_commands_node_status ON remote_commands(node_id, status);
ALTER TABLE remote_commands ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
