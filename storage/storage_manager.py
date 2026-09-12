"""StorageManager: MinIO / S3-compatible object storage wrapper.

Refactored from the original root-level module:
- no side effects at construction (bucket creation is lazy)
- settings and client are injectable (tests use fakes, no real MinIO needed)
- boto3 errors are wrapped in StorageError with actionable messages
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

from core.config import Settings, get_settings


class StorageError(Exception):
    """Raised when an object-storage operation fails."""


class StorageManager:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        s3_client: Optional[Any] = None,
        presign_client: Optional[Any] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.bucket = self._settings.minio_bucket_name
        self._s3_client = s3_client or self._build_client(self._settings.minio_endpoint)
        self._presign_client = presign_client
        self._bucket_ready = False
        self._lock = RLock()

    def _build_client(self, endpoint: str) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self._settings.minio_access_key,
            aws_secret_access_key=self._settings.minio_secret_key,
            region_name="us-east-1",
            config=Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}),
        )

    def _presigner(self) -> Any:
        """Client used only to sign download URLs.

        SigV4 signs the Host header, so a URL that must work from another machine
        has to be signed *with* that machine's address. Rewriting the host after
        signing would invalidate the signature.
        """
        if not self._settings.minio_public_endpoint:
            return self._s3_client
        if self._presign_client is None:
            self._presign_client = self._build_client(self._settings.minio_public_endpoint)
        return self._presign_client

    def ensure_bucket(self) -> None:
        """Idempotently create the bucket before the first write."""
        with self._lock:
            if self._bucket_ready:
                return
            try:
                self._s3_client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in ("404", "NoSuchBucket", "NotFound"):
                    raise StorageError(
                        f"cannot reach bucket '{self.bucket}': {exc}"
                    ) from exc
                try:
                    self._s3_client.create_bucket(Bucket=self.bucket)
                except ClientError as create_exc:
                    raise StorageError(
                        f"failed to create bucket '{self.bucket}': {create_exc}"
                    ) from create_exc
            self._bucket_ready = True

    def upload_file_content(self, object_name: str, content: bytes) -> str:
        """Upload bytes and return a presigned download URL."""
        self.ensure_bucket()
        try:
            self._s3_client.put_object(
                Bucket=self.bucket, Key=object_name, Body=content
            )
        except ClientError as exc:
            raise StorageError(f"upload failed '{object_name}': {exc}") from exc
        return self.generate_presigned_url(object_name)

    def download_file_content(self, object_name: str) -> bytes:
        """Download bytes from object storage."""
        try:
            response = self._s3_client.get_object(Bucket=self.bucket, Key=object_name)
            return response["Body"].read()
        except ClientError as exc:
            raise StorageError(f"download failed '{object_name}': {exc}") from exc

    def stat_object(self, object_name: str) -> Optional[dict[str, Any]]:
        """Return ``{"size": int, "etag": str}`` or None when the object is absent.

        Used by artifact validation: a task must not report success while the
        deliverable it claims to have produced is missing or empty.
        """
        try:
            response = self._s3_client.head_object(Bucket=self.bucket, Key=object_name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(f"stat failed '{object_name}': {exc}") from exc
        return {"size": int(response.get("ContentLength", 0)), "etag": response.get("ETag", "")}

    def generate_presigned_url(self, object_name: str, expires_in: int = 3600) -> str:
        """Generate a temporary download URL, signed for the public endpoint if set."""
        try:
            return self._presigner().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": object_name},
                ExpiresIn=expires_in,
            )
        except ClientError as exc:
            raise StorageError(f"presign failed '{object_name}': {exc}") from exc
