from pydantic_settings import BaseSettings, SettingsConfigDict

# 打包流水线会在 PyInstaller 前生成 app/_version.py；源码运行时回退为 "dev"。
try:
    from ._version import APP_VERSION
except Exception:  # pragma: no cover
    APP_VERSION = "dev"


class Settings(BaseSettings):
    app_env: str = "development"
    app_version: str = APP_VERSION
    data_dir: str = ""
    database_url: str = "sqlite:///./dev.db"
    redis_url: str = "redis://localhost:6379/0"
    task_execution_mode: str = "poll"
    orchestration_mode: str = "state_machine"
    celery_task_name: str = "app.celery_tasks.execute_persisted_task"
    celery_queue: str = "knowledge-tasks"
    celery_health_timeout_seconds: float = 0.5
    mqtt_host: str = "localhost"
    mqtt_port: int = 8883
    mqtt_tls_enabled: bool = False
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_ca_file: str = ""
    mqtt_client_cert_file: str = ""
    mqtt_client_key_file: str = ""
    mqtt_bridge_api_key: str = ""
    hub_internal_url: str = "http://hub:8000"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    node_offline_after_seconds: int = 90
    file_missing_after_seconds: int = 300
    remote_command_lease_seconds: int = 120
    task_lease_seconds: int = 120
    worker_poll_seconds: float = 2.0
    worker_id: str = "knowledge-worker"
    max_upload_bytes: int = 52_428_800
    parse_chunk_chars: int = 1000
    replica_node_ids: str = ""
    admin_api_key: str = ""
    node_api_key: str = ""
    mobile_api_key: str = ""
    embedding_provider: str = "ollama"
    embedding_base_url: str = "http://ollama:11434"
    embedding_model: str = "bge-large-zh"
    embedding_dimension: int = 1024
    embedding_timeout_seconds: float = 10.0
    embedding_allowed_hosts: str = "ollama,localhost,127.0.0.1"
    search_api_key: str = ""
    flywheel_ingest_api_key: str = ""
    download_api_key: str = ""
    max_search_query_chars: int = 4000
    max_search_top_k: int = 20
    knowledge_categories: str = "通用,财务,人力,制度"
    dify_base_url: str = ""
    dify_api_key: str = ""
    dify_dataset_id: str = ""
    rag_strict: bool = True
    rag_min_score: float = 0.0
    boost_enabled: bool = True
    boost_weight: float = 1.0
    gap_summary_interval_hours: float = 24.0
    dsh_enabled: bool = False
    dsh_base_url: str = ""
    dsh_api_key: str = ""
    dsh_timeout_seconds: float = 15.0
    dsh_allowed_hosts: str = "localhost,127.0.0.1,deepseek-harness"
    storage_backend: str = "local"
    storage_root: str = "./storage"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = ""
    s3_prefix: str = "knowledge-hub"
    alert_channels: str = ""
    alert_email_smtp_host: str = ""
    alert_email_smtp_port: int = 587
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_username: str = ""
    alert_email_password: str = ""
    alert_email_tls: bool = True
    alert_webhook_url: str = ""
    alert_wecom_webhook: str = ""
    alert_dingtalk_webhook: str = ""
    alert_retry_max: int = 3
    alert_retry_backoff_seconds: int = 30
    alert_suppress_window_seconds: int = 300
    security_secret: str = ""
    jwt_ttl_seconds: int = 3600
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_issuer: str = ""
    backup_dir: str = "./backups"
    backup_retention_days: int = 7

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def fixed_replica_node_ids(self) -> list[str]:
        return [item.strip() for item in self.replica_node_ids.split(",") if item.strip()]

    @property
    def embedding_allowed_hostnames(self) -> set[str]:
        return {item.strip().lower() for item in self.embedding_allowed_hosts.split(",") if item.strip()}

    @property
    def celery_enabled(self) -> bool:
        return self.task_execution_mode.strip().lower() in {"celery", "auto"}

    @property
    def langgraph_enabled(self) -> bool:
        return self.orchestration_mode.strip().lower() == "langgraph"

    @property
    def dsh_allowed_hostnames(self) -> set[str]:
        return {item.strip().lower() for item in self.dsh_allowed_hosts.split(",") if item.strip()}

    @property
    def enabled_alert_channels(self) -> list[str]:
        return [item.strip().lower() for item in self.alert_channels.split(",") if item.strip()]


settings = Settings()
