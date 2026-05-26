from __future__ import annotations

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings


def split_documents(docs: list[LCDocument]) -> list[LCDocument]:
    """Token-aware recursive split.

    Defaults to ~800 chars / 120 overlap — tune via env without touching code.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
        add_start_index=True,
    )
    return splitter.split_documents(docs)
