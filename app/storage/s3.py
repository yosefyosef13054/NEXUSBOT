"""S3-compatible storage (AWS S3, MinIO, Cloudflare R2, …) through boto3 in worker threads."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import settings


class S3Storage:
    backend = "s3"

    def __init__(self, *, bucket: str, client: Any) -> None:
        self.bucket = bucket
        self.client = client

    @classmethod
    def from_settings(cls) -> S3Storage:
        import boto3
        from botocore.config import Config

        if not settings.S3_BUCKET:
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET")
        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY or None,
            config=Config(s3={"addressing_style": "path" if settings.S3_FORCE_PATH_STYLE else "auto"}),
        )
        return cls(bucket=settings.S3_BUCKET, client=client)

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        def _put() -> None:
            with path.open("rb") as f:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=f, ContentType=content_type)
            path.unlink(missing_ok=True)

        await asyncio.to_thread(_put)

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(
            lambda: self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        )

    async def get_bytes(self, key: str) -> bytes:
        def _get() -> bytes:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read()

        return await asyncio.to_thread(_get)

    async def stream(self, key: str, *, chunk_size: int = 1 << 16) -> AsyncIterator[bytes]:
        obj = await asyncio.to_thread(lambda: self.client.get_object(Bucket=self.bucket, Key=key))
        body = obj["Body"]
        while True:
            chunk = await asyncio.to_thread(body.read, chunk_size)
            if not chunk:
                break
            yield chunk

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self.client.delete_object(Bucket=self.bucket, Key=key))

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception:  # noqa: BLE001 — botocore ClientError 404
                return False

        return await asyncio.to_thread(_head)

    @contextlib.asynccontextmanager
    async def local_path(self, key: str) -> AsyncIterator[Path]:
        suffix = Path(key).suffix
        tmp = Path(tempfile.mkstemp(suffix=suffix, prefix="nexusbot-")[1])
        try:
            await asyncio.to_thread(lambda: self.client.download_file(self.bucket, key, str(tmp)))
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)

    async def presigned_url(
        self, key: str, *, expires_seconds: int, filename: str | None = None
    ) -> str | None:
        if expires_seconds <= 0:
            return None
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
        return await asyncio.to_thread(
            lambda: self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_seconds)
        )
