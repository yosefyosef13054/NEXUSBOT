"""Object storage behind uploads, attachments and artifacts.

`get_storage()` returns the configured backend: local disk (`UPLOAD_DIR`) or any
S3-compatible bucket. Everything above this layer deals in *keys*
(`documents/<user>/<uuid>_<name>`), never paths, so API and workers can run on separate
hosts with S3 and tests run on a temp directory with the local backend.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.storage.base import Storage, storage_key
from app.storage.local import LocalStorage


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    if settings.STORAGE_BACKEND.lower() == "s3":
        from app.storage.s3 import S3Storage

        return S3Storage.from_settings()
    return LocalStorage(settings.UPLOAD_DIR)


def reset_storage() -> None:
    get_storage.cache_clear()


__all__ = ["Storage", "LocalStorage", "get_storage", "reset_storage", "storage_key"]
