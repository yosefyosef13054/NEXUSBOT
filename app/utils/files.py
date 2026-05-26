from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from app.config import settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    base = _SAFE.sub("_", os.path.basename(name)).strip("._") or "file"
    return base[:200]


def storage_path_for(user_id: str, name: str) -> Path:
    """Returns an absolute on-disk path namespaced by user."""
    root = Path(settings.UPLOAD_DIR) / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{uuid4().hex}_{safe_filename(name)}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
