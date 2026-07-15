from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

DecisionType = Literal[
    "direct_response",
    "clarification",
    "grounded_response",
    "handoff",
    "out_of_scope",
    "tool_calls",
]


class PlannedToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class OrchestratorDecision(BaseModel):
    type: DecisionType
    response: str = ""
    reason: str = ""
    tool_calls: list[PlannedToolCall] = Field(default_factory=list)


def decision_from_ai_message(
    message: AIMessage,
    *,
    has_successful_tool_results: bool,
) -> OrchestratorDecision:
    if message.tool_calls:
        return OrchestratorDecision(
            type="tool_calls",
            tool_calls=[
                PlannedToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    name=str(call["name"]),
                    arguments=dict(call.get("args") or {}),
                )
                for index, call in enumerate(message.tool_calls, start=1)
            ],
        )

    text = _message_content_to_text(message.content).strip()
    if not text:
        raise ValueError("orchestrator returned neither tool calls nor a final response")
    return OrchestratorDecision(
        type=("grounded_response" if has_successful_tool_results else "direct_response"),
        response=text,
    )


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return "" if content is None else str(content)
