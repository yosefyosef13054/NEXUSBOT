from __future__ import annotations

from pathlib import Path

from langchain_community.document_loaders import (
    BSHTMLLoader,
    Docx2txtLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredMarkdownLoader,
)
from langchain_core.documents import Document as LCDocument


_EXT_LOADERS = {
    ".pdf": PyPDFLoader,
    ".docx": Docx2txtLoader,
    ".md": UnstructuredMarkdownLoader,
    ".html": BSHTMLLoader,
    ".htm": BSHTMLLoader,
    ".txt": TextLoader,
    ".log": TextLoader,
    ".csv": TextLoader,
    ".json": TextLoader,
}


def load_file_as_documents(path: Path) -> list[LCDocument]:
    """Detect file type from extension and load into LangChain Documents.

    Adding a new format = a single new entry in `_EXT_LOADERS`.
    """
    suffix = path.suffix.lower()
    loader_cls = _EXT_LOADERS.get(suffix, TextLoader)
    loader = loader_cls(str(path)) if loader_cls is not TextLoader else TextLoader(str(path), autodetect_encoding=True)
    return loader.load()
