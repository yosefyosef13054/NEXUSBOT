from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import AsyncIterator

import aiofiles


class LocalStorage:
    """Files under a root directory. Keys are relative paths; absolute keys (legacy
    `Document.storage_path` values from before the storage layer) are honoured as-is."""

    backend = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, key: str) -> Path:
        if key.startswith("/"):
            return Path(key)
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("storage key escapes the storage root")
        return path

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        dest = self.path_for(key)

        def _move() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, dest)
            except OSError:
                shutil.copyfile(path, dest)
                path.unlink(missing_ok=True)

        await asyncio.to_thread(_move)

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        dest = self.path_for(key)
        await asyncio.to_thread(lambda: dest.parent.mkdir(parents=True, exist_ok=True))
        async with aiofiles.open(dest, "wb") as f:
            await f.write(data)

    async def get_bytes(self, key: str) -> bytes:
        async with aiofiles.open(self.path_for(key), "rb") as f:
            return await f.read()

    async def stream(self, key: str, *, chunk_size: int = 1 << 16) -> AsyncIterator[bytes]:
        async with aiofiles.open(self.path_for(key), "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    async def delete(self, key: str) -> None:
        path = self.path_for(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.path_for(key).exists)

    @contextlib.asynccontextmanager
    async def local_path(self, key: str) -> AsyncIterator[Path]:
        yield self.path_for(key)

    async def presigned_url(
        self, key: str, *, expires_seconds: int, filename: str | None = None
    ) -> str | None:
        return None
