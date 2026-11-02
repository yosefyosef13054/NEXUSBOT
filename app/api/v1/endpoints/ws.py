from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, Callable
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, UnauthorizedError
from app.core.logger import get_logger
from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.services import chat_service
from app.services.auth_service import get_user_by_api_key, get_user_by_token

router = APIRouter(tags=["websocket"])
logger = get_logger(__name__)

AUTH_FRAME_TIMEOUT_SECONDS = 10


async def _authenticate_ws(db: AsyncSession, token: str | None, api_key: str | None) -> User:
    if token:
        return await get_user_by_token(db, token)
    if api_key:
        return await get_user_by_api_key(db, api_key)
    raise UnauthorizedError("Missing credentials")


async def _auth_from_first_frame(websocket: WebSocket, db: AsyncSession) -> User:
    """Credentials arrive in the first frame, not the URL, so they never land in access logs."""
    raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_FRAME_TIMEOUT_SECONDS)
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UnauthorizedError("First frame must be an auth JSON frame") from e
    if not isinstance(msg, dict) or msg.get("type") != "auth":
        raise UnauthorizedError("First frame must be {type: 'auth', token | api_key}")
    return await _authenticate_ws(db, msg.get("token"), msg.get("api_key"))


def _error(code: str, message: str) -> dict[str, Any]:
    return {"type": "error", "data": {"code": code, "message": message}}


def _uuid_list(value: Any) -> list[UUID] | None:
    if value is None:
        return None
    try:
        return [UUID(str(v)) for v in value]
    except (TypeError, ValueError):
        return None


@router.websocket("/ws/sessions/{session_id}")
async def chat_ws(
    websocket: WebSocket,
    session_id: UUID,
    token: str | None = Query(default=None),
    api_key: str | None = Query(default=None),
):
    """Bidirectional chat stream.

    Client → server frames (JSON):
        { "type": "auth", "token": "<jwt>" }            first frame (or "api_key")
        { "type": "user_message", "content": "...", "use_rag": true, "use_tools": false,
          "tool_names": null, "model": null, "collection_ids": null }
        { "type": "approval", "approval_id": "...", "decision": "approve" | "deny",
          "tool_call_ids": null }
        { "type": "stop" }                               cancel the in-flight generation
        { "type": "ping" }

    Server → client frames (JSON):
        { "type": "auth_ok" | "token" | "citations" | "tool_call" | "usage" | "approval_required"
                 | "done" | "assistant_message" | "error" | "pong", "data": ... }

    Query-string credentials (`?token=`) are still accepted for older clients but logged
    as deprecated.
    """
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        try:
            if token or api_key:
                logger.warning("ws_query_string_auth_deprecated", session_id=str(session_id))
                user = await _authenticate_ws(db, token, api_key)
            else:
                user = await _auth_from_first_frame(websocket, db)
        except AppException as e:
            await websocket.send_json(_error(e.code, e.message))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        except TimeoutError:
            await websocket.send_json(_error("auth_timeout", "No auth frame received"))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        except WebSocketDisconnect:
            return

        await websocket.send_json({"type": "auth_ok", "data": {"user_id": str(user.id)}})

        send_lock = asyncio.Lock()
        generation: asyncio.Task | None = None
        stop_event: asyncio.Event | None = None

        async def _send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def _run(
            events_factory: Callable[[asyncio.Event], AsyncIterator[dict[str, Any]]], stop: asyncio.Event
        ) -> None:
            try:
                async for evt in events_factory(stop):
                    await _send(evt)
            except AppException as e:
                await db.rollback()
                await _send(_error(e.code, e.message))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("ws_chat_failed")
                await db.rollback()
                with contextlib.suppress(Exception):
                    await _send(_error("internal_error", str(e)[:300]))

        def _message_events(msg: dict[str, Any]):
            def factory(stop: asyncio.Event):
                return chat_service.stream_message(
                    db,
                    user_id=user.id,
                    session_id=session_id,
                    content=str(msg.get("content") or "").strip(),
                    use_rag=bool(msg.get("use_rag", True)),
                    use_tools=bool(msg.get("use_tools", False)),
                    tool_names=msg.get("tool_names"),
                    model=msg.get("model"),
                    collection_ids=_uuid_list(msg.get("collection_ids")),
                    user_default_model=user.default_model,
                    temperature=msg.get("temperature"),
                    max_tokens=msg.get("max_tokens"),
                    stop_event=stop,
                )

            return factory

        def _approval_events(msg: dict[str, Any]):
            def factory(stop: asyncio.Event):
                return chat_service.resolve_approval(
                    db,
                    user_id=user.id,
                    session_id=session_id,
                    approval_id=UUID(str(msg.get("approval_id"))),
                    decision="approve" if msg.get("decision") == "approve" else "deny",
                    tool_call_ids=msg.get("tool_call_ids"),
                    stop_event=stop,
                )

            return factory

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send(_error("bad_request", "Invalid JSON"))
                    continue
                if not isinstance(msg, dict):
                    await _send(_error("bad_request", "Frame must be a JSON object"))
                    continue

                kind = msg.get("type")
                if kind == "ping":
                    await _send({"type": "pong"})
                elif kind == "stop":
                    if generation is not None and not generation.done() and stop_event is not None:
                        stop_event.set()
                elif kind in ("user_message", "approval"):
                    if generation is not None and not generation.done():
                        await _send(_error("busy", "A generation is already in progress"))
                        continue
                    if kind == "user_message":
                        if not str(msg.get("content") or "").strip():
                            await _send(_error("bad_request", "Empty content"))
                            continue
                        factory = _message_events(msg)
                    else:
                        try:
                            UUID(str(msg.get("approval_id")))
                        except ValueError:
                            await _send(_error("bad_request", "approval_id must be a UUID"))
                            continue
                        factory = _approval_events(msg)
                    stop_event = asyncio.Event()
                    generation = asyncio.create_task(_run(factory, stop_event))
                else:
                    await _send(_error("bad_request", "Unknown message type"))
        except WebSocketDisconnect:
            pass
        finally:
            if generation is not None and not generation.done():
                generation.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await generation
