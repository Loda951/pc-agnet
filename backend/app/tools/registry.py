from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.llm import build_chat_model
from app.tools.catalog import CatalogQueryPlanner, CatalogToolService, LLMCatalogQueryPlanner
from app.tools.knowledge import KnowledgeRetrievalToolService
from app.tools.orders import OrderToolService
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogSearchInput,
    DocumentSearchInput,
    OrderLookupInput,
    ToolError,
    ToolExecutionResult,
)


class ToolRegistryError(ValueError):
    pass


ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


class RegisteredTool:
    def __init__(
        self,
        name: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
    ):
        self.name = name
        self.input_model = input_model
        self.handler = handler


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ToolRegistryError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(name, input_model, handler)

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    async def execute(self, name: str, input_data: dict[str, Any]) -> ToolExecutionResult:
        tool = self._tools.get(name)
        if tool is None:
            return _error_result(name, "unknown_tool", f"unknown tool: {name}")

        try:
            request = tool.input_model.model_validate(input_data)
        except ValidationError as exc:
            return _error_result(name, "invalid_input", exc.errors())

        try:
            output = await tool.handler(request)
        except Exception as exc:  # pragma: no cover - defensive boundary for orchestration
            return _error_result(name, type(exc).__name__, str(exc))

        return ToolExecutionResult(
            tool_name=name,
            ok=True,
            output=output.model_dump(mode="json"),
        )


def build_catalog_planner(settings: Settings | None = None) -> CatalogQueryPlanner | None:
    settings = settings or get_settings()
    if not settings.catalog_llm_planner_enabled:
        return None

    chat_model = build_chat_model(settings)
    if chat_model is None:
        return None

    return LLMCatalogQueryPlanner(chat_model)


def build_tool_registry(
    session: AsyncSession,
    catalog_planner: CatalogQueryPlanner | None = None,
    settings: Settings | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    catalog = CatalogToolService(
        session,
        planner=catalog_planner or build_catalog_planner(settings),
    )
    orders = OrderToolService(session)
    knowledge = KnowledgeRetrievalToolService()

    registry.register(
        "catalog.search",
        CatalogSearchInput,
        catalog.search,  # type: ignore[arg-type]
    )
    registry.register(
        "catalog.compare",
        CatalogCompareInput,
        catalog.compare,  # type: ignore[arg-type]
    )
    registry.register(
        "order.lookup",
        OrderLookupInput,
        orders.lookup,  # type: ignore[arg-type]
    )
    registry.register(
        "policy.search",
        DocumentSearchInput,
        knowledge.search_policy,  # type: ignore[arg-type]
    )
    registry.register(
        "knowledge.search",
        DocumentSearchInput,
        knowledge.search_knowledge,  # type: ignore[arg-type]
    )
    return registry


def _error_result(name: str, code: str, message: Any) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name=name,
        ok=False,
        error=ToolError(code=code, message=str(message)),
    )
