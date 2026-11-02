from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import auth, chat, collections, documents, health, models, sessions, ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(models.router)
api_router.include_router(sessions.router)
api_router.include_router(chat.router)
api_router.include_router(chat.approvals_router)
api_router.include_router(chat.search_router)
api_router.include_router(collections.router)
api_router.include_router(documents.router)
api_router.include_router(ws.router)
