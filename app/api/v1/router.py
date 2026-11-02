from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    assistants,
    attachments,
    audio,
    auth,
    chat,
    collections,
    devices,
    documents,
    health,
    mcp,
    memories,
    models,
    orgs,
    sessions,
    share,
    usage,
    ws,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(orgs.router)
api_router.include_router(usage.router)
api_router.include_router(admin.router)
api_router.include_router(models.router)
api_router.include_router(assistants.router)
api_router.include_router(memories.router)
api_router.include_router(sessions.router)
api_router.include_router(chat.router)
api_router.include_router(chat.approvals_router)
api_router.include_router(chat.generations_router)
api_router.include_router(chat.search_router)
api_router.include_router(attachments.router)
api_router.include_router(audio.router)
api_router.include_router(audio.voice_router)
api_router.include_router(share.router)
api_router.include_router(share.public_router)
api_router.include_router(mcp.router)
api_router.include_router(devices.router)
api_router.include_router(collections.router)
api_router.include_router(documents.router)
api_router.include_router(ws.router)
