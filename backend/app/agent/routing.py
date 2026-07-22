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
RouteCapability = Literal[
    "catalog_search",
    "catalog_compare",
    "catalog_facets",
    "order_lookup",
    "policy_search",
    "knowledge_search",
    "planner_required",
]
TaskArtifact = Literal[
    "products",
    "ranked_product",
    "comparison",
    "facets",
    "order",
    "documents",
]


class TaskInputRequirement(BaseModel):
    """A typed input that Runtime must bind before executing a dependent task."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    source: Literal["context_product", "comparison_context", "task_output"]
    task_id: str | None = Field(default=None, pattern=r"^sq_[1-9][0-9]*$")

    @model_validator(mode="after")
    def validate_source(self) -> "TaskInputRequirement":
        if self.source == "task_output" and self.task_id is None:
            raise ValueError("task_output input requires task_id")
        if self.source in {"context_product", "comparison_context"} and self.task_id is not None:
            raise ValueError("context input cannot declare task_id")
        return self


class TaskResultSelector(BaseModel):
    """A deterministic projection applied to one task's Tool result."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sales_rank"]
    rank: int = Field(ge=1, le=20)
    scope: Literal["spu", "sku"] = "spu"


class RoutedSubquery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^sq_[1-9][0-9]*$", max_length=24)
    query: str = Field(min_length=1, max_length=1000)
    disposition: RouteDisposition
    reason_code: str = Field(min_length=1, max_length=100)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    clarification_question: str = Field(default="", max_length=500)
    capability: RouteCapability | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    input_requirements: list[TaskInputRequirement] = Field(default_factory=list, max_length=8)
    produces: TaskArtifact | None = None
    result_selector: TaskResultSelector | None = None

    @model_validator(mode="after")
    def validate_clarification(self) -> "RoutedSubquery":
        if self.disposition == "clarification" and not self.clarification_question.strip():
            raise ValueError("clarification subquery requires clarification_question")
        if self.disposition != "tool_planning" and self.capability is not None:
            raise ValueError("only tool_planning subqueries may declare a capability")
        if self.disposition != "tool_planning" and (
            self.depends_on
            or self.input_requirements
            or self.produces is not None
            or self.result_selector is not None
        ):
            raise ValueError("only tool_planning subqueries may declare task graph fields")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("task dependencies must be unique")
        if self.id in self.depends_on:
            raise ValueError("task cannot depend on itself")
        for requirement in self.input_requirements:
            if requirement.task_id is not None and requirement.task_id not in self.depends_on:
                raise ValueError("task_output input must reference a declared dependency")
        if self.result_selector is not None and self.produces != "ranked_product":
            raise ValueError("result_selector requires produces=ranked_product")
        return self


class RequestRoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_query: str = Field(min_length=1, max_length=2000)
    subqueries: list[RoutedSubquery] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_task_graph(self) -> "RequestRoutePlan":
        ids = [item.id for item in self.subqueries]
        if len(ids) != len(set(ids)):
            raise ValueError("subquery ids must be unique")
        tasks = {item.id: item for item in self.subqueries}
        for item in self.subqueries:
            for dependency_id in item.depends_on:
                dependency = tasks.get(dependency_id)
                if dependency is None:
                    raise ValueError("task dependency must reference an existing task")
                if dependency.disposition != "tool_planning":
                    raise ValueError("tool task cannot depend on a blocked task")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task graph must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in tasks[task_id].depends_on:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
        return self


def request_route_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "route_request",
            "description": (
                "Rewrite the current request with trusted conversation context, split it into "
                "semantic tasks with explicit dependencies, and classify every task before "
                "business tools."
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


def ready_tool_subqueries(
    plan: RequestRoutePlan | Mapping[str, Any] | None,
    *,
    usable_task_ids: set[str] | None = None,
    attempted_task_ids: set[str] | None = None,
) -> list[RoutedSubquery]:
    """Return unattempted tasks whose declared dependencies have usable evidence."""
    usable = usable_task_ids or set()
    attempted = attempted_task_ids or set()
    return [
        item
        for item in tool_planning_subqueries(plan)
        if item.id not in attempted and set(item.depends_on) <= usable
    ]


__all__ = [
    "RequestRoutePlan",
    "RouteCapability",
    "RouteDisposition",
    "RoutedSubquery",
    "TaskArtifact",
    "TaskInputRequirement",
    "TaskResultSelector",
    "blocked_subqueries",
    "request_route_tool_definition",
    "route_plan_from_ai_message",
    "ready_tool_subqueries",
    "tool_planning_subqueries",
]
