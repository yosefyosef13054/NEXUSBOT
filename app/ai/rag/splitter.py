"""Structure-aware chunking.

- Markdown-style headings become a section path ("2. Scope > 2.1 Goals") carried in chunk
  metadata; page numbers from PDF loaders are preserved.
- Every chunk records its character offsets within its page/document so citations can
  point at the exact span.
- With RAG_CONTEXT_HEADERS the text that gets *embedded* is prefixed with
  "document > section" (stored separately as `embed_text`), which markedly improves
  recall for chunks that only make sense in context; the stored content stays raw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_MAX_SECTION_DEPTH = 4


@dataclass(slots=True)
class Section:
    path: str | None
    text: str
    offset: int  # start of `text` within the page/document


def split_sections(text: str) -> list[Section]:
    """Split on markdown headings, keeping a breadcrumb path; one section when none."""
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [Section(path=None, text=text, offset=0)]
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append(Section(path=None, text=preamble, offset=0))
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = " ".join(m.group(2).split())
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack[-_MAX_SECTION_DEPTH:])
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end]
        if body.strip():
            sections.append(Section(path=path, text=body, offset=body_start))
        else:
            # Heading with no body of its own: keep the title so it is still searchable.
            sections.append(Section(path=path, text=title, offset=m.start(2)))
    return sections


def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
        add_start_index=True,
    )


def split_documents(docs: list[LCDocument], *, document_name: str | None = None) -> list[LCDocument]:
    """Chunk loader output into retrieval units with page/section/offset metadata."""
    splitter = _splitter()
    out: list[LCDocument] = []
    for doc in docs:
        raw_page = doc.metadata.get("page")
        page = (int(raw_page) + 1) if isinstance(raw_page, int) else None  # PyPDF pages are 0-based
        source = doc.metadata.get("source")
        for section in split_sections(doc.page_content):
            for piece in splitter.create_documents([section.text]):
                content = piece.page_content.strip()
                if not content:
                    continue
                start = section.offset + int(piece.metadata.get("start_index", 0))
                header = " > ".join(p for p in (document_name, section.path) if p)
                embed_text = f"{header}\n{content}" if settings.RAG_CONTEXT_HEADERS and header else content
                out.append(
                    LCDocument(
                        page_content=content,
                        metadata={
                            "page": page,
                            "section": section.path,
                            "start_index": start,
                            "end_index": start + len(piece.page_content),
                            "source": source,
                            "embed_text": embed_text,
                        },
                    )
                )
    return out
