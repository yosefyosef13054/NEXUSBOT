from __future__ import annotations

from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import build_chat_llm
from app.ai.memory.long_term import load_session_summary, save_session_summary
from app.ai.prompts.templates import SUMMARIZER_PROMPT
from app.config import settings
from app.models.chat import ChatMessage, MessageRole
from app.utils.tokens import count_tokens


async def maybe_compact_history(session: AsyncSession, *, session_id: UUID) -> None:
    """If the conversation exceeds the token budget, compress older turns into a summary.

    Strategy: keep the most recent N messages verbatim, summarize everything older
    by *appending* to the existing summary.
    """
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.deleted_at.is_(None),
            ChatMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
        )
        .order_by(ChatMessage.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    if len(rows) <= settings.CHAT_MAX_HISTORY_MESSAGES:
        return

    overflow = rows[: -settings.CHAT_MAX_HISTORY_MESSAGES]
    text = "\n".join(f"{r.role.value.upper()}: {r.content}" for r in overflow)
    if count_tokens(text) < settings.CHAT_SUMMARY_TRIGGER_TOKENS:
        return

    existing = await load_session_summary(session, session_id=session_id)
    llm = build_chat_llm(streaming=False, temperature=0)
    chain = SUMMARIZER_PROMPT | llm
    new_summary = (await chain.ainvoke({"summary": existing or "(none)", "turns": text})).content
    await save_session_summary(session, session_id=session_id, summary=str(new_summary).strip())
