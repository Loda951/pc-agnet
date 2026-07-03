from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.catalog import CatalogRepository
from app.schemas.catalog import ProductSearchRequest


@pytest.mark.asyncio
async def test_search_matches_wireless_headphones_boolean_attribute(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        products = await CatalogRepository(session).search_products(
            ProductSearchRequest(
                query="Codex",
                category="耳机",
                max_price=Decimal("400"),
                filters={"connection_type": "Wireless"},
                limit=5,
            )
        )

    assert [product.title for product in products] == [
        "SteelSeries Codex Arctis Nova Wireless Black"
    ]
    assert products[0].specs["wireless"] == "是"
    assert products[0].specs["microphone"] == "是"


@pytest.mark.asyncio
async def test_search_ranks_compare_query_by_individual_product_terms(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    async with db_session_factory() as session:
        products = await CatalogRepository(session).search_products(
            ProductSearchRequest(query="Codex G502 Viper 对比", category="鼠标", limit=5)
        )

    titles = [product.title for product in products]
    assert "Logitech Codex G502 Hero Black" in titles
    assert "Razer Codex Viper V3 Pro White" in titles
    assert titles.index("Logitech Codex G502 Hero Black") < 3
    assert titles.index("Razer Codex Viper V3 Pro White") < 3
