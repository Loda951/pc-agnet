from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

RouteDisposition = Literal[
    "tool_planning",
    "direct_response",
    "clarification",
    "human_handoff",
    "out_of_scope",
    "unsupported",
    "security_refusal",
]


class RoutedSubquery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^sq_[1-9][0-9]*$", max_length=24)
    query: str = Field(min_length=1, max_length=1000)
    disposition: RouteDisposition
    reason_code: str = Field(min_length=1, max_length=100)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    clarification_question: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_clarification(self) -> "RoutedSubquery":
        if self.disposition == "clarification" and not self.clarification_question.strip():
            raise ValueError("clarification subquery requires clarification_question")
        return self


class RequestRoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_query: str = Field(min_length=1, max_length=2000)
    subqueries: list[RoutedSubquery] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_unique_subquery_ids(self) -> "RequestRoutePlan":
        ids = [item.id for item in self.subqueries]
        if len(ids) != len(set(ids)):
            raise ValueError("subquery ids must be unique")
        return self


def request_route_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "route_request",
            "description": (
                "Rewrite the current request with trusted conversation context, split it into "
                "self-contained subqueries, and classify every subquery before business tools."
            ),
            "parameters": RequestRoutePlan.model_json_schema(),
        },
    }


def route_plan_from_ai_message(message: AIMessage) -> RequestRoutePlan:
    if len(message.tool_calls) != 1:
        raise ValueError("request router must return exactly one route_request tool call")
    call = message.tool_calls[0]
    if str(call.get("name") or "") != "route_request":
        raise ValueError("request router returned an unexpected tool call")
    arguments = call.get("args")
    if not isinstance(arguments, Mapping):
        raise TypeError("route_request arguments must be an object")
    return RequestRoutePlan.model_validate(dict(arguments))


def tool_planning_subqueries(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
) -> list[RoutedSubquery]:
    if plan is None:
        return []
    validated = (
        plan
        if isinstance(plan, RequestRoutePlan)
        else RequestRoutePlan.model_validate(dict(plan))
    )
    return [item for item in validated.subqueries if item.disposition == "tool_planning"]


def blocked_subqueries(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
) -> list[RoutedSubquery]:
    if plan is None:
        return []
    validated = (
        plan
        if isinstance(plan, RequestRoutePlan)
        else RequestRoutePlan.model_validate(dict(plan))
    )
    return [item for item in validated.subqueries if item.disposition != "tool_planning"]


__all__ = [
    "RequestRoutePlan",
    "RouteDisposition",
    "RoutedSubquery",
    "blocked_subqueries",
    "request_route_tool_definition",
    "route_plan_from_ai_message",
    "tool_planning_subqueries",
]
