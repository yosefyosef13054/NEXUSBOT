from __future__ import annotations

from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import Runnable

from app.ai.llm import build_chat_llm
from app.ai.prompts.templates import build_chat_prompt


def build_rag_chain(
    *,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = True,
) -> Runnable:
    """Retrieval-augmented chain. Inputs: { input, history, summary, context }."""
    prompt = build_chat_prompt(system_prompt=system_prompt, use_rag=True, use_tools=False)
    llm = build_chat_llm(temperature=temperature, max_tokens=max_tokens, streaming=streaming)
    return prompt | llm | StrOutputParser()
