"""Drives one agent turn through the LangGraph graph: start, detect interrupts, resume."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.types import Command

from app.ai.agents.graph import AgentConfig, build_agent_graph


@dataclass
class ToolRun:
    call_id: str
    tool: str
    input: dict[str, Any]
    output: str
    status: str  # ok | error | denied
    latency_ms: int = 0


@dataclass
class AgentOutcome:
    final_text: str = ""
    interrupted: bool = False
    approval: dict[str, Any] | None = None
    tool_runs: list[ToolRun] = field(default_factory=list)
    messages: list[BaseMessage] = field(default_factory=list)


def _text(message: BaseMessage) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)


def extract_tool_runs(messages: list[BaseMessage]) -> list[ToolRun]:
    """Pair each ToolMessage with the AIMessage tool call that produced it."""
    calls: dict[str, Mapping[str, Any]] = {}
    runs: list[ToolRun] = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls[str(tc.get("id"))] = tc
        elif isinstance(m, ToolMessage):
            call: Mapping[str, Any] = calls.get(str(m.tool_call_id)) or {}
            extra = m.additional_kwargs or {}
            runs.append(
                ToolRun(
                    call_id=str(m.tool_call_id),
                    tool=m.name or call.get("name") or "unknown",
                    input=extra.get("input") or call.get("args") or {},
                    output=_text(m),
                    status=extra.get("nexusbot_status") or ("ok" if m.status == "success" else "error"),
                    latency_ms=int(extra.get("latency_ms") or 0),
                )
            )
    return runs


class AgentRunner:
    """One turn of the agent, identified by `thread_id` in the checkpointer."""

    def __init__(
        self,
        *,
        llm: BaseChatModel,
        tools: list[BaseTool],
        approval_required: set[str],
        thread_id: str,
        checkpointer: Any,
        callbacks: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        max_steps: int = 8,
        tool_timeout_seconds: float = 60.0,
    ) -> None:
        self.thread_id = thread_id
        self.graph = build_agent_graph(
            AgentConfig(
                llm=llm,
                tools=tools,
                approval_required=approval_required,
                max_steps=max_steps,
                tool_timeout_seconds=tool_timeout_seconds,
            ),
            checkpointer=checkpointer,
        )
        self.config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "callbacks": callbacks or [],
            "metadata": metadata or {},
            "run_name": "nexusbot.agent",
        }

    async def start(self, messages: list[BaseMessage]) -> AgentOutcome:
        output = await self.graph.ainvoke(
            {"messages": messages, "steps": 0, "decisions": {}}, config=self.config
        )
        return await self._outcome(output)

    async def resume(self, decision: dict[str, Any]) -> AgentOutcome:
        output = await self.graph.ainvoke(Command(resume=decision), config=self.config)
        return await self._outcome(output)

    async def _outcome(self, output: dict[str, Any]) -> AgentOutcome:
        messages: list[BaseMessage] = list(output.get("messages") or [])
        approval: dict[str, Any] | None = None
        interrupts = output.get("__interrupt__")
        if interrupts:
            approval = dict(interrupts[0].value)
        else:
            snapshot = await self.graph.aget_state(self.config)
            for task in getattr(snapshot, "tasks", ()) or ():
                for intr in getattr(task, "interrupts", ()) or ():
                    approval = dict(intr.value)
                    break
                if approval:
                    break
            if approval is None and snapshot is not None:
                messages = list((snapshot.values or {}).get("messages") or messages)
        final_text = ""
        if approval is None:
            for m in reversed(messages):
                if isinstance(m, AIMessage) and not m.tool_calls:
                    final_text = _text(m).strip()
                    break
        return AgentOutcome(
            final_text=final_text,
            interrupted=approval is not None,
            approval=approval,
            tool_runs=extract_tool_runs(messages),
            messages=messages,
        )
