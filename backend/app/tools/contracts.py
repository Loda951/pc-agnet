import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.tools.registry import ToolRegistry, build_tool_registry
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogCompareOutput,
    CatalogSearchInput,
    CatalogSearchOutput,
    DocumentSearchInput,
    DocumentSearchOutput,
    OrderLookupInput,
    OrderLookupOutput,
    ToolError,
    ToolExecutionResult,
)


class ToolRuntimeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class OrderLookupPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int | None = None
    limit: int = Field(default=5, ge=1, le=20)


class ToolContract(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_name: str
    registry_name: str
    description: str
    public_input_model: type[BaseModel]
    internal_input_model: type[BaseModel]
    output_model: type[BaseModel]
    runtime_fields: tuple[str, ...] = ()
    read_only: bool = True
    parallel_safe: bool = False
    requires_auth: bool = False
    timeout_seconds: float | None = 12.0

    @property
    def name(self) -> str:
        """Backward-compatible alias used by the current orchestrator adapter."""
        return self.llm_name

    def as_llm_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.llm_name,
                "description": self.description,
                "parameters": self.public_input_model.model_json_schema(),
            },
        }


class ToolContractProvider(Protocol):
    def list_contracts(self) -> Sequence[ToolContract]: ...

    def get_contract(self, llm_name: str) -> ToolContract | None: ...


class ToolExecutor(Protocol):
    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult: ...


class StaticToolContractProvider:
    def __init__(self, contracts: Sequence[ToolContract] | None = None):
        configured = contracts or default_tool_contracts()
        self._contracts = {contract.llm_name: contract for contract in configured}
        if len(self._contracts) != len(configured):
            raise ValueError("duplicate tool llm_name in contracts")

    def list_contracts(self) -> Sequence[ToolContract]:
        return tuple(self._contracts.values())

    def get_contract(self, llm_name: str) -> ToolContract | None:
        return self._contracts.get(llm_name)


class RegistryToolExecutor:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        registry: ToolRegistry | None = None,
    ):
        self.registry = registry or build_tool_registry(session, settings=settings)

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        try:
            public_input = contract.public_input_model.model_validate(arguments)
            internal_input = public_input.model_dump(mode="json", exclude_none=True)
            runtime = ToolRuntimeContext.model_validate(runtime_context)
            for field_name in contract.runtime_fields:
                internal_input[field_name] = getattr(runtime, field_name)
            validated_internal = contract.internal_input_model.model_validate(internal_input)
        except ValidationError as exc:
            return _error_result(contract.llm_name, "invalid_input", str(exc))
        except AttributeError as exc:
            return _error_result(contract.llm_name, "invalid_input", str(exc))

        async def run_tool() -> ToolExecutionResult:
            return await self.registry.execute(
                contract.registry_name,
                validated_internal.model_dump(mode="json", exclude_none=True),
            )

        try:
            if contract.timeout_seconds is None:
                result = await run_tool()
            else:
                result = await asyncio.wait_for(run_tool(), timeout=contract.timeout_seconds)
        except TimeoutError:
            return _error_result(contract.llm_name, "timeout", "tool execution timed out")
        except Exception as exc:  # pragma: no cover - defensive boundary
            return _error_result(contract.llm_name, "execution_error", str(exc))

        result.tool_name = contract.llm_name
        if result.ok and result.output is not None:
            try:
                contract.output_model.model_validate(result.output)
            except ValidationError as exc:
                return _error_result(contract.llm_name, "execution_error", str(exc))
        return result


LLM_SAFE_TOOL_NAMES = (
    "catalog_search",
    "catalog_compare",
    "order_lookup",
    "policy_search",
    "knowledge_search",
)


def default_tool_contracts() -> tuple[ToolContract, ...]:
    return (
        ToolContract(
            llm_name="catalog_search",
            registry_name="catalog.search",
            description=(
                "Search the current PC peripheral catalog for product recommendations, "
                "category, brand, budget, connection type, stock, price, and specification "
                "facts. Use catalog_compare instead for product-versus-product comparisons. "
                "Do not use this tool for policies or order data."
            ),
            public_input_model=CatalogSearchInput,
            internal_input_model=CatalogSearchInput,
            output_model=CatalogSearchOutput,
            timeout_seconds=15.0,
        ),
        ToolContract(
            llm_name="catalog_compare",
            registry_name="catalog.compare",
            description=(
                "Compare PC peripheral products or explicit SKU IDs using catalog facts such "
                "as price, stock, brand, category, sales count, and specifications. Return "
                "comparison evidence only; do not make final purchase promises."
            ),
            public_input_model=CatalogCompareInput,
            internal_input_model=CatalogCompareInput,
            output_model=CatalogCompareOutput,
            timeout_seconds=18.0,
        ),
        ToolContract(
            llm_name="order_lookup",
            registry_name="order.lookup",
            description=(
                "Look up the authenticated user's recent orders or one explicit order. The "
                "runtime injects the authenticated user_id; never ask the model to provide or "
                "override user identity."
            ),
            public_input_model=OrderLookupPublicInput,
            internal_input_model=OrderLookupInput,
            output_model=OrderLookupOutput,
            runtime_fields=("user_id",),
            requires_auth=True,
            timeout_seconds=8.0,
        ),
        ToolContract(
            llm_name="policy_search",
            registry_name="policy.search",
            description=(
                "Search read-only store policies for returns, refunds, warranty, invoices, "
                "shipping, price protection, and after-sales procedures. Return policy "
                "evidence only; do not approve refunds or perform after-sales actions."
            ),
            public_input_model=DocumentSearchInput,
            internal_input_model=DocumentSearchInput,
            output_model=DocumentSearchOutput,
            timeout_seconds=8.0,
        ),
        ToolContract(
            llm_name="knowledge_search",
            registry_name="knowledge.search",
            description=(
                "Search read-only PC peripheral, brand, merchant, purchasing, and FAQ "
                "knowledge. Do not use it for current product price, stock, or order facts."
            ),
            public_input_model=DocumentSearchInput,
            internal_input_model=DocumentSearchInput,
            output_model=DocumentSearchOutput,
            timeout_seconds=8.0,
        ),
    )


def _error_result(name: str, code: str, message: Any) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name=name,
        ok=False,
        error=ToolError(code=code, message=str(message)),
    )
