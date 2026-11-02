from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from uuid import uuid4

from app.config import settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Extensions that are plain text under the hood (sniffed as UTF-8/ASCII text).
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".log", ".json", ".html", ".htm"}

MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
}


def safe_filename(name: str) -> str:
    base = _SAFE.sub("_", os.path.basename(name)).strip("._") or "file"
    return base[:200]


def storage_path_for(user_id: str, name: str) -> Path:
    """A scratch path for an incoming upload (validated and hashed here, then handed to
    the storage backend, which moves or uploads it)."""
    root = Path(settings.UPLOAD_DIR) / "tmp" / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{uuid4().hex}_{safe_filename(name)}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def sha256_file_async(path: Path) -> str:
    return await asyncio.to_thread(sha256_file, path)


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return True
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # Allow a truncated multi-byte sequence at the end of the sample.
        try:
            head[:-3].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False


def sniff_mime(head: bytes, extension: str) -> str | None:
    """Detect the real content type from the first bytes of a file.

    Returns the detected mime type, or None when the bytes do not match the claimed
    extension (e.g. an executable renamed to `.pdf`). Client-supplied `Content-Type`
    headers are never trusted.
    """
    ext = extension.lower()
    if ext == ".pdf":
        return "application/pdf" if head.startswith(b"%PDF-") else None
    if ext == ".docx":
        # OOXML is a zip container; the central directory lives at the end, so only the
        # local-file-header signature is checkable from the head.
        return MIME_BY_EXTENSION[".docx"] if head.startswith(b"PK\x03\x04") else None
    if ext in _TEXT_EXTENSIONS:
        if not _looks_like_text(head):
            return None
        if ext == ".json":
            try:
                json.loads(head.decode("utf-8", errors="ignore") if len(head) < 4096 else "[]")
            except ValueError:
                # A large JSON file won't parse from a prefix; accept text-looking content.
                pass
        return MIME_BY_EXTENSION.get(ext, "text/plain")
    return None


def is_valid_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return "[Content_Types].xml" in z.namelist()
    except zipfile.BadZipFile:
        return False
