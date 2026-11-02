from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import AsyncIterator, Protocol
from uuid import uuid4

from app.utils.files import safe_filename


def storage_key(namespace: str, owner_id: str, name: str) -> str:
    """`<namespace>/<owner>/<uuid>_<safe name>` — unique, namespaced, filesystem-safe."""
    return f"{namespace}/{owner_id}/{uuid4().hex}_{safe_filename(name)}"


class Storage(Protocol):
    backend: str

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        """Store a local file under `key` (the local file may be moved)."""

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get_bytes(self, key: str) -> bytes: ...

    def stream(self, key: str, *, chunk_size: int = 1 << 16) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    def local_path(self, key: str) -> AbstractAsyncContextManager[Path]:
        """A readable local path for `key` (a temp copy on remote backends)."""
        ...

    async def presigned_url(
        self, key: str, *, expires_seconds: int, filename: str | None = None
    ) -> str | None:
        """A direct download URL when the backend supports it, else None (proxy the bytes)."""
        ...
