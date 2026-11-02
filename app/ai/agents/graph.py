"""LangGraph ReAct agent with human-in-the-loop approvals and per-turn budgets.

Graph:  START → agent ─┬─(tool calls needing approval)→ gate → tools → agent
                       ├─(tool calls)──────────────────────→ tools → agent
                       └─(no tool calls / budget exhausted)→ END

`gate` calls `interrupt()` with the pending tool calls; the run halts (state is in the
checkpointer) until the user answers and the graph is resumed with
`Command(resume={"decision": "approve" | "deny", "tool_call_ids": [...] | None})`.
Denied calls get a ToolMessage explaining the denial so the model can carry on without
the tool.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

BUDGET_EXHAUSTED_NOTE = (
    "You have used all the tool calls allowed for this turn. Answer the user now with what "
    "you have, stating clearly what you could not verify."
)
TOOL_OUTPUT_MAX_CHARS = 8000


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    # {tool_call_id: "approve" | "deny"} for calls that went through the gate.
    decisions: dict[str, str]


@dataclass
class AgentConfig:
    llm: BaseChatModel
    tools: list[BaseTool]
    approval_required: set[str] = field(default_factory=set)
    max_steps: int = 8
    tool_timeout_seconds: float = 60.0


def _text(message: BaseMessage) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)


def approval_payload(
    tool_calls: Sequence[Mapping[str, Any]], tool_map: dict[str, BaseTool]
) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "call_id": tc.get("id"),
                "tool": tc["name"],
                "input": tc.get("args") or {},
                "description": getattr(tool_map.get(tc["name"]), "description", ""),
            }
            for tc in tool_calls
        ]
    }


def build_agent_graph(cfg: AgentConfig, *, checkpointer: Any = None):
    tool_map: dict[str, BaseTool] = {t.name: t for t in cfg.tools}
    llm_with_tools = cfg.llm.bind_tools(cfg.tools) if cfg.tools else cfg.llm

    async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        steps = state.get("steps", 0)
        messages = state["messages"]
        if steps >= cfg.max_steps:
            final = await cfg.llm.ainvoke(
                messages + [SystemMessage(content=BUDGET_EXHAUSTED_NOTE)], config=config
            )
            # Never honor tool calls past the budget.
            stripped = AIMessage(
                content=_text(final),
                usage_metadata=getattr(final, "usage_metadata", None),
                response_metadata=getattr(final, "response_metadata", {}) or {},
            )
            return {"messages": [stripped], "steps": steps + 1}
        response = await llm_with_tools.ainvoke(messages, config=config)
        return {"messages": [response], "steps": steps + 1}

    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if any(tc["name"] in cfg.approval_required for tc in last.tool_calls):
                return "gate"
            return "tools"
        return END

    async def gate_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        pending = [tc for tc in last.tool_calls if tc["name"] in cfg.approval_required]
        answer = interrupt(approval_payload(pending, tool_map))
        decisions = dict(state.get("decisions") or {})
        chosen = str((answer or {}).get("decision", "deny"))
        ids = (answer or {}).get("tool_call_ids") or [tc.get("id") for tc in pending]
        for tc in pending:
            decisions[str(tc.get("id"))] = chosen if tc.get("id") in ids else "deny"
        return {"decisions": decisions}

    async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        decisions = state.get("decisions") or {}

        async def run_one(tc: Mapping[str, Any]) -> ToolMessage:
            call_id = str(tc.get("id"))
            name = tc["name"]
            started = time.time()

            def done(content: str, status: str) -> ToolMessage:
                return ToolMessage(
                    content=content[:TOOL_OUTPUT_MAX_CHARS],
                    tool_call_id=call_id,
                    name=name,
                    status="success" if status == "ok" else "error",
                    additional_kwargs={
                        "latency_ms": int((time.time() - started) * 1000),
                        "nexusbot_status": status,
                        "input": tc.get("args") or {},
                    },
                )

            if decisions.get(call_id) == "deny":
                return done("The user declined to run this tool. Continue without it.", "denied")
            tool = tool_map.get(name)
            if tool is None:
                return done(f"Unknown tool '{name}'.", "error")
            try:
                output = await asyncio.wait_for(
                    tool.ainvoke(tc.get("args") or {}, config=config), timeout=cfg.tool_timeout_seconds
                )
                return done(str(getattr(output, "content", output)), "ok")
            except TimeoutError:
                return done(f"Tool '{name}' timed out after {cfg.tool_timeout_seconds:.0f}s.", "error")
            except Exception as e:  # noqa: BLE001
                return done(f"Tool '{name}' failed: {str(e)[:500]}", "error")

        results = await asyncio.gather(*(run_one(tc) for tc in last.tool_calls))
        return {"messages": list(results)}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("gate", gate_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"gate": "gate", "tools": "tools", END: END})
    graph.add_edge("gate", "tools")
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
