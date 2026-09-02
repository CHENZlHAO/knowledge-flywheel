"""后台配置总控台的运行时覆盖层：数据库优先于环境变量。

白名单键可经后台配置总控台写入；敏感键（dify_api_key）在 API 回显时只返回掩码。
"""
from sqlalchemy import select

from .db import SessionLocal
from .models import SettingsOverride

# 允许通过后台配置写入的键（类型标注），其余键一律拒绝
WRITABLE_KEYS: dict[str, str] = {
    "dify_base_url": "str",
    "dify_api_key": "secret",
    "dify_dataset_id": "str",
    "rag_strict": "bool",
    "rag_min_score": "float",
    "boost_enabled": "bool",
    "boost_weight": "float",
    "gap_summary_interval_hours": "float",
    "knowledge_categories": "str",
}

SECRET_KEYS = {"dify_api_key"}


def get_effective_setting(db, key: str, default=None):
    """读取一个键：后台覆盖优先，未覆盖时返回 default（调用方回退到环境变量）。"""
    row = db.scalars(select(SettingsOverride).where(SettingsOverride.key == key)).first()
    if row is None:
        return default
    value = row.value
    kind = WRITABLE_KEYS.get(key, "str")
    try:
        if kind == "bool":
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if kind == "float":
            return float(value)
        if kind == "int":
            return int(value)
    except (TypeError, ValueError):
        return default
    return value


def list_overrides(db) -> dict:
    """返回全部覆盖（敏感键掩码化），供后台配置总控台展示。"""
    out: dict[str, str] = {}
    for row in db.scalars(select(SettingsOverride)).all():
        if row.key in SECRET_KEYS and row.value:
            out[row.key] = mask_secret(row.value)
        else:
            out[row.key] = row.value
    return out


def set_override(db, key: str, value: str) -> None:
    """写入（或更新）一个白名单键。空字符串视为清除覆盖。"""
    if key not in WRITABLE_KEYS:
        raise ValueError(f"key not writable: {key}")
    row = db.scalars(select(SettingsOverride).where(SettingsOverride.key == key)).first()
    if not value:
        if row is not None:
            db.delete(row)
            db.commit()
        return
    if row is None:
        row = SettingsOverride(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * 6 + value[-4:]


def get_db_session():
    return SessionLocal()
