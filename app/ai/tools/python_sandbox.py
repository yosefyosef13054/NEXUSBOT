"""`run_python`: execute model-written Python in a resource-limited subprocess.

Files the code writes under `./output/` come back as artifacts (download cards in the
client). Attached files can be copied into `./input/`. The tool is off by default
(`CODE_EXECUTION_ENABLED`) and always requires the user's approval: rlimits and a clean
environment bound CPU, memory and disk, but they are not a security boundary against a
hostile model on a shared host — run the API in a disposable container for that.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Type
from uuid import UUID

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from app.ai.tools.context import ToolContext
from app.config import settings

MAX_OUTPUT_CHARS = 10_000


class RunPythonInput(BaseModel):
    code: str = Field(description="Python 3 source to run. Print results; save files under ./output/.")
    attachment_ids: list[str] = Field(
        default_factory=list,
        description="Ids of chat attachments to copy into ./input/ (CSV, text, images…) before running.",
    )


def _limits() -> None:  # pragma: no cover - runs in the child process
    import resource

    mem = settings.CODE_EXECUTION_MEMORY_MB * 1024 * 1024
    cpu = settings.CODE_EXECUTION_TIMEOUT_SECONDS
    fsize = settings.CODE_EXECUTION_MAX_FILE_MB * 1024 * 1024
    for res, value in (
        (resource.RLIMIT_AS, (mem, mem)),
        (resource.RLIMIT_CPU, (cpu, cpu + 1)),
        (resource.RLIMIT_FSIZE, (fsize, fsize)),
        (resource.RLIMIT_NPROC, (64, 64)),
    ):
        try:
            resource.setrlimit(res, value)
        except (ValueError, OSError):
            pass
    os.setsid()


class PythonSandboxTool(BaseTool):
    name: str = "run_python"
    description: str = (
        "Run Python code in a sandboxed subprocess (no network guarantees, limited CPU/memory, "
        f"{settings.CODE_EXECUTION_TIMEOUT_SECONDS}s timeout). Print what you want to see; files saved under "
        "./output/ are returned to the user as downloadable artifacts. Attached files can be copied into ./input/."
    )
    args_schema: Type[BaseModel] = RunPythonInput
    ctx: ToolContext

    def _run(self, code: str, attachment_ids: list[str] | None = None) -> str:  # pragma: no cover
        raise NotImplementedError("run_python is async only")

    async def _arun(self, code: str, attachment_ids: list[str] | None = None, **_: Any) -> str:
        if not settings.CODE_EXECUTION_ENABLED:
            return "Error: code execution is disabled on this server."
        workdir = Path(tempfile.mkdtemp(prefix="nexusbot-sandbox-"))
        try:
            (workdir / "input").mkdir()
            (workdir / "output").mkdir()
            copied = await self._copy_inputs(workdir / "input", attachment_ids or [])
            (workdir / "main.py").write_text(code, encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONHASHSEED": "0",
                "HOME": str(workdir),
                "TMPDIR": str(workdir),
                "MPLBACKEND": "Agg",
            }
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "main.py",
                cwd=str(workdir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_limits if os.name == "posix" else None,
            )
            timed_out = False
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.CODE_EXECUTION_TIMEOUT_SECONDS + 2
                )
            except TimeoutError:
                timed_out = True
                with contextlib_suppress():
                    os.killpg(proc.pid, 9)
                with contextlib_suppress():
                    proc.kill()
                out_b, err_b = b"", b""
                with contextlib_suppress():
                    await proc.wait()
            stdout = out_b.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
            stderr = err_b.decode("utf-8", errors="replace")[-MAX_OUTPUT_CHARS:]
            artifacts = await self._collect_artifacts(workdir / "output")
            parts: list[str] = []
            if copied:
                parts.append(f"Input files: {', '.join(copied)}")
            if timed_out:
                parts.append(f"Error: execution timed out after {settings.CODE_EXECUTION_TIMEOUT_SECONDS}s.")
            elif proc.returncode:
                parts.append(f"Exit code {proc.returncode}.")
            if stdout.strip():
                parts.append(f"stdout:\n{stdout.rstrip()}")
            if stderr.strip() and (proc.returncode or timed_out):
                parts.append(f"stderr:\n{stderr.rstrip()}")
            if artifacts:
                parts.append(
                    "Artifacts: " + ", ".join(f"{a['name']} ({a['size_bytes']} bytes)" for a in artifacts)
                )
            if not parts:
                parts.append("(no output)")
            return "\n\n".join(parts)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    async def _copy_inputs(self, dest: Path, ids: list[str]) -> list[str]:
        if not ids:
            return []
        from app.db.session import db_session
        from app.models.attachment import Attachment
        from app.storage import get_storage

        names: list[str] = []
        async with db_session() as db:
            for raw in ids[:10]:
                try:
                    att = await db.get(Attachment, UUID(str(raw)))
                except ValueError:
                    continue
                if att is None or att.deleted_at is not None or att.user_id != self.ctx.user_id:
                    continue
                if self.ctx.session_id is not None and att.session_id != self.ctx.session_id:
                    continue
                data = await get_storage().get_bytes(att.storage_key)
                target = dest / Path(att.name).name
                await asyncio.to_thread(target.write_bytes, data)
                names.append(target.name)
        return names

    async def _collect_artifacts(self, output_dir: Path) -> list[dict[str, Any]]:
        sink = getattr(self.ctx, "artifacts", None)
        files = sorted(p for p in output_dir.rglob("*") if p.is_file())[
            : settings.CODE_EXECUTION_MAX_OUTPUT_FILES
        ]
        records: list[dict[str, Any]] = []
        max_bytes = settings.CODE_EXECUTION_MAX_FILE_MB * 1024 * 1024
        for path in files:
            size = path.stat().st_size
            if size == 0 or size > max_bytes:
                continue
            data = await asyncio.to_thread(path.read_bytes)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if sink is None:
                records.append({"name": path.name, "size_bytes": size, "mime_type": mime})
                continue
            records.append(
                await sink.add(name=path.name, data=data, mime_type=mime, description="run_python output")
            )
        return records


class contextlib_suppress:
    """Tiny `contextlib.suppress(Exception)` without the import noise above."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True
