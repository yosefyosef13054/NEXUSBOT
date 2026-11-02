from __future__ import annotations

from pathlib import Path

from langchain_community.document_loaders import (
    BSHTMLLoader,
    Docx2txtLoader,
    PyPDFLoader,
    TextLoader,
)
from langchain_core.documents import Document as LCDocument

# Markdown, CSV, JSON and logs are loaded as plain text: the recursive splitter handles
# their structure well enough, and it keeps the heavyweight `unstructured` dependency out.
_EXT_LOADERS = {
    ".pdf": PyPDFLoader,
    ".docx": Docx2txtLoader,
    ".html": BSHTMLLoader,
    ".htm": BSHTMLLoader,
}


def load_file_as_documents(path: Path) -> list[LCDocument]:
    """Detect file type from extension and load into LangChain Documents.

    Adding a new format = a single new entry in `_EXT_LOADERS`.
    """
    suffix = path.suffix.lower()
    loader_cls = _EXT_LOADERS.get(suffix)
    if loader_cls is None:
        loader = TextLoader(str(path), autodetect_encoding=True)
    else:
        loader = loader_cls(str(path))
    return loader.load()
