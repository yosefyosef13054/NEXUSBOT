"""Turn message text + attachments into model input (LangChain content parts)."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from app.services.attachment_service import AttachmentInput


def _file_block(att: AttachmentInput) -> str:
    body = (att.text or "").strip()
    if not body:
        return f"[Attached {att.kind} «{att.name}» ({att.mime_type}) — no readable text]"
    label = "Transcript of attached audio" if att.kind == "audio" else "Attached file"
    return f"{label} «{att.name}»:\n```\n{body}\n```"


def build_user_content(text: str, attachments: list[AttachmentInput]) -> str | list[str | dict[Any, Any]]:
    """A plain string when there are no images; otherwise OpenAI-style content parts, which
    langchain-openai / -anthropic / -google-genai all translate for their providers."""
    blocks = [_file_block(a) for a in attachments if a.kind != "image"]
    combined = "\n\n".join([*blocks, text.strip()]) if blocks else text
    images = [a for a in attachments if a.kind == "image" and a.data_url]
    if not images:
        return combined
    parts: list[str | dict[Any, Any]] = []
    if combined.strip():
        parts.append({"type": "text", "text": combined})
    for img in images:
        parts.append({"type": "image_url", "image_url": {"url": img.data_url}})
    return parts


def build_user_message(text: str, attachments: list[AttachmentInput]) -> HumanMessage:
    return HumanMessage(content=build_user_content(text, attachments))
