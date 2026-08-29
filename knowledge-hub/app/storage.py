"""Pluggable object storage for binary file content.

Text parsing keeps its existing UTF-8 path. Binary uploads (PDF, Word, images,
archives, ...) are stored through this adapter as opaque bytes and are served by
the separately keyed download gateway.

The default backend is the local filesystem for development and single-host
deployments. ``STORAGE_BACKEND=s3`` selects an S3-compatible backend (AWS S3,
MinIO, or any compatible gateway) and is import-safe: boto3 is only imported when
the S3 backend is actually selected.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from .config import settings


class StorageBackend(Protocol):
    name: str

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


def _safe_key(file_hash: str) -> str:
    # file_hash is already hex, but keep the key strictly within one safe form.
    if len(file_hash) < 16:
        raise ValueError("file hash too short for a storage key")
    return f"{file_hash[:2]}/{file_hash[2:4]}/{file_hash}"


class LocalStorage:
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root not in candidate.parents:
            raise ValueError("storage key escapes the storage root")
        return candidate

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    name = "s3"

    def __init__(self):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - requires boto3 installed
            raise RuntimeError("boto3 is required for the S3 storage backend") from exc
        client_kwargs: dict = {}
        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_region:
            client_kwargs["region_name"] = settings.s3_region
        if settings.s3_access_key:
            client_kwargs["aws_access_key_id"] = settings.s3_access_key
        if settings.s3_secret_key:
            client_kwargs["aws_secret_access_key"] = settings.s3_secret_key
        self.client = boto3.client("s3", **client_kwargs)
        self.bucket = settings.s3_bucket
        self.prefix = settings.s3_prefix.rstrip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


def get_storage_for(name: str) -> StorageBackend:
    if name.strip().lower() == "s3":
        return S3Storage()
    return LocalStorage(settings.storage_root)


def get_storage() -> StorageBackend:
    return get_storage_for(settings.storage_backend)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
