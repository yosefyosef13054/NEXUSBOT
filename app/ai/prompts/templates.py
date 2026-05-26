from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.ai.prompts.system_prompts import (
    DEFAULT_ASSISTANT_V1,
    RAG_GROUNDING_V1,
    SUMMARIZER_V1,
    TITLE_V1,
    TOOL_USE_V1,
)


def build_chat_prompt(
    *,
    system_prompt: str | None = None,
    use_rag: bool = False,
    use_tools: bool = False,
) -> ChatPromptTemplate:
    """Compose the chat prompt from the requested capabilities."""
    parts: list[str] = [system_prompt or DEFAULT_ASSISTANT_V1]
    if use_rag:
        parts.append(RAG_GROUNDING_V1)
        parts.append("Retrieved context:\n{context}")
    if use_tools:
        parts.append(TOOL_USE_V1)
    if "summary" not in "".join(parts):
        parts.append("Conversation summary (if any):\n{summary}")

    return ChatPromptTemplate.from_messages(
        [
            ("system", "\n\n".join(parts)),
            MessagesPlaceholder("history"),
            ("human", "{input}"),
        ]
    )


SUMMARIZER_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SUMMARIZER_V1),
        ("human", "Existing summary:\n{summary}\n\nNew turns:\n{turns}"),
    ]
)

TITLE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", TITLE_V1),
        ("human", "{first_message}"),
    ]
)
