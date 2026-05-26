from __future__ import annotations

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.runnables import Runnable

from app.ai.llm import build_chat_llm
from app.ai.prompts.templates import build_chat_prompt
from app.ai.tools.registry import get_tools


def build_tool_agent(
    *,
    system_prompt: str | None = None,
    tool_names: list[str] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = True,
    use_rag: bool = False,
) -> Runnable:
    """Build a tool-calling agent.

    The agent expects the same inputs as the chat chain plus `agent_scratchpad`
    which the executor injects automatically. RAG can be combined with tools
    by setting `use_rag=True` (the retrieved `context` is passed in by the orchestrator).
    """
    tools = get_tools(tool_names)
    llm = build_chat_llm(temperature=temperature, max_tokens=max_tokens, streaming=streaming)
    prompt = build_chat_prompt(system_prompt=system_prompt, use_rag=use_rag, use_tools=True)

    agent = create_openai_tools_agent(llm=llm, tools=tools, prompt=prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=False,
        max_iterations=6,
        return_intermediate_steps=True,
        handle_parsing_errors=True,
    )
