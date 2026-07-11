from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.tools.registry import ToolRegistry, build_tool_registry
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogSearchInput,
    DocumentSearchInput,
    ToolExecutionResult,
)


class OrderLookupPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int | None = None
    limit: int = Field(default=5, ge=1, le=20)


class ToolContract(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    registry_name: str
    description: str
    public_input_model: type[BaseModel]
    runtime_fields: tuple[str, ...] = ()

    def as_llm_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.public_input_model.model_json_schema(),
            },
        }


class ToolContractProvider(Protocol):
    def list_contracts(self) -> Sequence[ToolContract]: ...

    def get_contract(self, name: str) -> ToolContract | None: ...


class ToolExecutor(Protocol):
    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult: ...


class StaticToolContractProvider:
    def __init__(self, contracts: Sequence[ToolContract] | None = None):
        configured = contracts or _default_contracts()
        self._contracts = {contract.name: contract for contract in configured}

    def list_contracts(self) -> Sequence[ToolContract]:
        return tuple(self._contracts.values())

    def get_contract(self, name: str) -> ToolContract | None:
        return self._contracts.get(name)


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
        public_input = contract.public_input_model.model_validate(arguments)
        internal_input = public_input.model_dump(mode="json", exclude_none=True)
        for field_name in contract.runtime_fields:
            internal_input[field_name] = runtime_context[field_name]
        return await self.registry.execute(contract.registry_name, internal_input)


def _default_contracts() -> tuple[ToolContract, ...]:
    return (
        ToolContract(
            name="catalog_search",
            registry_name="catalog.search",
            description=(
                "Search the PC peripheral catalog for product recommendations, filters, prices, "
                "stock, brands, and specifications. Use it whenever the answer requires current "
                "catalog facts."
            ),
            public_input_model=CatalogSearchInput,
        ),
        ToolContract(
            name="catalog_compare",
            registry_name="catalog.compare",
            description=(
                "Compare PC peripheral products or explicit SKU IDs using catalog facts. "
                "Use it for product-versus-product questions and purchase comparisons."
            ),
            public_input_model=CatalogCompareInput,
        ),
        ToolContract(
            name="order_lookup",
            registry_name="order.lookup",
            description=(
                "Look up the authenticated user's recent orders or one explicit order. "
                "The authenticated user identity is injected by the runtime."
            ),
            public_input_model=OrderLookupPublicInput,
            runtime_fields=("user_id",),
        ),
        ToolContract(
            name="policy_search",
            registry_name="policy.search",
            description=(
                "Search read-only store policies covering returns, refunds, warranty, invoices, "
                "shipping, price protection, and after-sales procedures."
            ),
            public_input_model=DocumentSearchInput,
        ),
        ToolContract(
            name="knowledge_search",
            registry_name="knowledge.search",
            description=(
                "Search read-only PC peripheral, brand, merchant, purchasing, and FAQ knowledge. "
                "Do not use it for current product price, stock, or order facts."
            ),
            public_input_model=DocumentSearchInput,
        ),
    )
