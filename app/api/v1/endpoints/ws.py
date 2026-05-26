from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, UnauthorizedError
from app.core.logger import get_logger
from app.db.session import AsyncSessionLocal
from app.services import chat_service
from app.services.auth_service import get_user_by_api_key, get_user_by_token

router = APIRouter(tags=["websocket"])
logger = get_logger(__name__)


async def _authenticate_ws(db: AsyncSession, token: str | None, api_key: str | None):
    if token:
        return await get_user_by_token(db, token)
    if api_key:
        return await get_user_by_api_key(db, api_key)
    raise UnauthorizedError("Missing credentials")


@router.websocket("/ws/sessions/{session_id}")
async def chat_ws(
    websocket: WebSocket,
    session_id: UUID,
    token: str | None = Query(default=None),
    api_key: str | None = Query(default=None),
):
    """Bidirectional chat stream.

    Client → server frames (JSON):
        { "type": "user_message", "content": "...", "use_rag": true, "use_tools": false }

    Server → client frames (JSON):
        { "type": "token" | "citations" | "tool_call" | "usage" | "done" | "assistant_message" | "error", "data": ... }
    """
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        try:
            user = await _authenticate_ws(db, token, api_key)
        except AppException as e:
            await websocket.send_json({"type": "error", "data": {"code": e.code, "message": e.message}})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "data": {"message": "Invalid JSON"}})
                    continue

                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if msg.get("type") != "user_message":
                    await websocket.send_json({"type": "error", "data": {"message": "Unknown message type"}})
                    continue

                content = (msg.get("content") or "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "data": {"message": "Empty content"}})
                    continue

                try:
                    async for evt in chat_service.stream_message(
                        db,
                        user_id=user.id,
                        session_id=session_id,
                        content=content,
                        use_rag=bool(msg.get("use_rag", True)),
                        use_tools=bool(msg.get("use_tools", False)),
                        tool_names=msg.get("tool_names"),
                        temperature=msg.get("temperature"),
                        max_tokens=msg.get("max_tokens"),
                    ):
                        await websocket.send_json(evt)
                except AppException as e:
                    await websocket.send_json({"type": "error", "data": {"code": e.code, "message": e.message}})
                except Exception as e:  # noqa: BLE001
                    logger.exception("ws_chat_failed")
                    await websocket.send_json({"type": "error", "data": {"message": str(e)[:300]}})
        except WebSocketDisconnect:
            return
