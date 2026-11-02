"""Importing this package registers every ORM model with `Base.metadata`.

Alembic env imports `register_models` so autogenerate sees every table.
"""

from app.models.chat import ChatMessage, ChatSession, MessageRole  # noqa: F401
from app.models.document import Document, DocumentChunk, DocumentStatus  # noqa: F401
from app.models.tool_call import ToolCall  # noqa: F401
from app.models.usage import AIUsageLog  # noqa: F401
from app.models.user import ApiKey, RefreshToken, User  # noqa: F401


def register_models() -> None:
    """No-op marker — importing this module is enough to register models."""
    return
