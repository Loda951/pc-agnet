import json
import re
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

STREAMABLE_DECISION_TYPES = {
    "direct_response",
    "clarification",
    "grounded_response",
}
TEMPLATE_DECISION_TYPES = {"handoff", "out_of_scope"}
TERMINAL_DECISION_TYPES = STREAMABLE_DECISION_TYPES | TEMPLATE_DECISION_TYPES
TYPE_HEADER_PATTERN = re.compile(r"^TYPE:\s*([a-z_]+)\s*$", re.IGNORECASE)
TYPE_HEADER_SEPARATOR_PATTERN = re.compile(r"\r?\n\r?\n")
MAX_TYPE_HEADER_LENGTH = 128


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
    has_tool_results: bool,
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
    header_decision = decision_from_type_content(text)
    if header_decision is not None:
        return header_decision
    payload = _extract_json_object(text)
    if payload is not None:
        decision = OrchestratorDecision.model_validate(payload)
        if decision.type == "tool_calls":
            raise ValueError("tool_calls must be returned through native tool calling")
        return decision

    return OrchestratorDecision(
        type="grounded_response" if has_tool_results else "direct_response",
        response=text,
    )


class TerminalResponseStreamParser:
    def __init__(self):
        self._header_buffer = ""
        self._response_parts: list[str] = []
        self.decision_type: str | None = None

    @property
    def has_streamable_response(self) -> bool:
        return self.decision_type in STREAMABLE_DECISION_TYPES

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        if self.decision_type is not None:
            if self.has_streamable_response:
                self._response_parts.append(text)
                return [text]
            return []

        self._header_buffer += text
        separator = TYPE_HEADER_SEPARATOR_PATTERN.search(self._header_buffer)
        if separator is None:
            if len(self._header_buffer) > MAX_TYPE_HEADER_LENGTH:
                raise ValueError("orchestrator TYPE header is missing or too long")
            return []

        header = self._header_buffer[: separator.start()]
        body = self._header_buffer[separator.end() :]
        self.decision_type = _parse_type_header(header)
        self._header_buffer = ""
        if self.has_streamable_response and body:
            self._response_parts.append(body)
            return [body]
        return []

    def finish(self) -> OrchestratorDecision:
        if self.decision_type is None:
            decision = decision_from_type_content(self._header_buffer.strip())
            if decision is None:
                raise ValueError("orchestrator did not return a valid TYPE header")
            return decision
        return OrchestratorDecision(
            type=self.decision_type,
            response="".join(self._response_parts).strip(),
        )


def decision_from_type_content(text: str) -> OrchestratorDecision | None:
    if not text:
        return None
    lines = text.replace("\r\n", "\n").split("\n", 1)
    try:
        decision_type = _parse_type_header(lines[0])
    except ValueError:
        return None
    response = lines[1].lstrip("\n").strip() if len(lines) > 1 else ""
    return OrchestratorDecision(type=decision_type, response=response)


def _parse_type_header(header: str) -> str:
    match = TYPE_HEADER_PATTERN.fullmatch(header.strip())
    if match is None or match.group(1).lower() not in TERMINAL_DECISION_TYPES:
        raise ValueError("invalid orchestrator TYPE header")
    return match.group(1).lower()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
