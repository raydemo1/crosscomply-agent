"""Immutable original-material storage backed by an S3-compatible client."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol


class ObjectResponse(Protocol):
    def read(self) -> bytes: ...

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class S3CompatibleClient(Protocol):
    def bucket_exists(self, bucket: str) -> bool: ...

    def make_bucket(self, bucket: str) -> None: ...

    def put_object(
        self,
        bucket: str,
        key: str,
        stream: BytesIO,
        size: int,
        **kwargs: object,
    ) -> object: ...

    def get_object(self, bucket: str, key: str) -> ObjectResponse: ...


@dataclass(frozen=True)
class StoredOriginal:
    object_key: str
    sha256: str
    byte_size: int


class MaterialObjectStore:
    """Store originals under immutable version-and-content-addressed keys."""

    def __init__(self, *, client: S3CompatibleClient, bucket: str) -> None:
        if not bucket.strip():
            raise ValueError("MinIO bucket 不能为空")
        self._client = client
        self._bucket = bucket

    def initialize(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def put_original(
        self,
        *,
        case_id: str,
        logical_name: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> StoredOriginal:
        if not content:
            raise ValueError("原件内容不能为空")
        self.initialize()
        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(filename).suffix.lower()
        safe_case_id = _safe_segment(case_id)
        safe_logical_name = _safe_segment(logical_name)
        object_key = f"cases/{safe_case_id}/materials/{safe_logical_name}/{digest}{suffix}"
        self._client.put_object(
            self._bucket,
            object_key,
            BytesIO(content),
            len(content),
            content_type=content_type or "application/octet-stream",
        )
        return StoredOriginal(object_key=object_key, sha256=digest, byte_size=len(content))

    def get_original(self, object_key: str) -> bytes:
        response = self._client.get_object(self._bucket, object_key)
        try:
            return response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            release = getattr(response, "release_conn", None)
            if callable(release):
                release()


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    if not normalized:
        normalized = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]
    return normalized


def material_object_store_from_env() -> MaterialObjectStore:
    """Build the production MinIO client from deployment-only environment values."""

    from minio import Minio

    endpoint = os.getenv("CROSSCOMPLY_MINIO_ENDPOINT", "minio:9000")
    access_key = os.getenv("CROSSCOMPLY_MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("CROSSCOMPLY_MINIO_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise RuntimeError("MinIO 访问凭据尚未配置")
    client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=os.getenv("CROSSCOMPLY_MINIO_SECURE", "false").lower() == "true",
    )
    return MaterialObjectStore(
        client=client,
        bucket=os.getenv("CROSSCOMPLY_MINIO_BUCKET", "crosscomply-materials"),
    )
