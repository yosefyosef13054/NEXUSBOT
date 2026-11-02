from __future__ import annotations

from pathlib import Path

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_core.documents import Document as LCDocument

from app.utils.web import html_to_text


class HtmlTextLoader:
    """HTML → readable text (scripts/styles/navigation dropped, one line per block)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> list[LCDocument]:
        body = Path(self.path).read_bytes()
        title = None
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(body[:200_000], "html.parser")
            if soup.title and soup.title.string:
                title = " ".join(soup.title.string.split())
        except Exception:  # noqa: BLE001
            title = None
        text = html_to_text(body)
        if title and not text.startswith(title):
            text = f"# {title}\n\n{text}"
        return [LCDocument(page_content=text, metadata={"source": self.path, "title": title})]


# Markdown, CSV, JSON and logs are loaded as plain text: the recursive splitter handles
# their structure well enough, and it keeps the heavyweight `unstructured` dependency out.
_EXT_LOADERS = {
    ".pdf": PyPDFLoader,
    ".docx": Docx2txtLoader,
    ".html": HtmlTextLoader,
    ".htm": HtmlTextLoader,
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
