from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.catalog import CATEGORY_ALIASES, CatalogRepository
from app.schemas.catalog import ProductSearchRequest, ProductSpecCondition


def test_category_aliases_include_compact_catalog_categories() -> None:
    assert CATEGORY_ALIASES["speakers"] == "音箱"
    assert CATEGORY_ALIASES["speaker"] == "音箱"
    assert CATEGORY_ALIASES["音箱"] == "音箱"
    assert CATEGORY_ALIASES["webcam"] == "摄像头"
    assert CATEGORY_ALIASES["webcams"] == "摄像头"
    assert CATEGORY_ALIASES["摄像头"] == "摄像头"


def test_search_statement_pushes_excluded_brands_into_sql() -> None:
    from app.repositories import catalog as catalog_repository

    statement = catalog_repository._catalog_search_statement(
        ProductSearchRequest(query="mouse", excluded_brands=["Logitech", "Razer"])
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert sql.count("brand.name NOT ILIKE") == 2


def test_usage_exclusions_are_applied_before_result_limit() -> None:
    from app.repositories import catalog as catalog_repository
    from app.schemas.catalog import ProductCard

    ranked = [
        ProductCard(
            spu_id=index,
            sku_id=index,
            title=f"Gaming Mouse {index}",
            brand="Other",
            category="mouse",
            price="99.00",
            stock=1,
            specs={"usage": "gaming"},
        )
        for index in range(1, 21)
    ]
    ranked.append(
        ProductCard(
            spu_id=21,
            sku_id=21,
            title="Office Mouse",
            brand="Other",
            category="mouse",
            price="109.00",
            stock=1,
            specs={"usage": "office"},
        )
    )

    selected = catalog_repository._take_eligible_products(
        ranked,
        excluded_usage=["gaming"],
        limit=3,
    )

    assert [product.sku_id for product in selected] == [21]


def test_positive_usage_is_required_before_result_limit() -> None:
    from app.repositories import catalog as catalog_repository
    from app.schemas.catalog import ProductCard

    products = [
        ProductCard(
            spu_id=1,
            sku_id=1,
            title="Generic Keyboard",
            brand="Other",
            category="keyboard",
            price="99.00",
            stock=1,
            specs={"switches": "Silent Red"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=2,
            title="Office Keyboard",
            brand="Other",
            category="keyboard",
            price="109.00",
            stock=1,
            specs={"usage": "office"},
        ),
    ]

    selected = catalog_repository._take_eligible_products(
        products,
        excluded_usage=[],
        usage_scenario="office",
        limit=3,
    )

    assert [product.sku_id for product in selected] == [2]


def test_usage_spec_requirements_are_applied_before_result_limit() -> None:
    from app.repositories import catalog as catalog_repository
    from app.schemas.catalog import ProductCard

    products = [
        ProductCard(
            spu_id=1,
            sku_id=1,
            title="Webcam without microphone",
            brand="Other",
            category="webcam",
            price="99.00",
            stock=1,
            specs={"microphone": "否", "frame_rate": "90fps"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=2,
            title="Meeting Webcam",
            brand="Other",
            category="webcam",
            price="109.00",
            stock=1,
            specs={"microphone": "是", "frame_rate": "60fps"},
        ),
    ]

    selected = catalog_repository._take_eligible_products(
        products,
        excluded_usage=[],
        required_conditions=[
            ProductSpecCondition(key="microphone", operator="eq", values=["是"])
        ],
        limit=3,
    )

    assert [product.sku_id for product in selected] == [2]


def test_usage_spec_condition_supports_numeric_ranges_and_exact_values() -> None:
    from app.repositories import catalog as catalog_repository

    specs = {"refresh_rate": "165Hz", "switches": "静音红轴"}

    assert catalog_repository._matches_spec_condition(
        specs,
        ProductSpecCondition(key="refresh_rate", operator="gte", values=["144"]),
    )
    assert catalog_repository._matches_spec_condition(
        specs,
        ProductSpecCondition(key="switches", operator="exact", values=["静音红轴"]),
    )
    assert not catalog_repository._matches_spec_condition(
        specs,
        ProductSpecCondition(key="switches", operator="exact", values=["线性红轴"]),
    )


@pytest.mark.asyncio
async def test_usage_exclusion_pages_past_fully_excluded_first_batch_without_postgres() -> None:
    from app.repositories import catalog as catalog_repository
    from app.schemas.catalog import ProductCard

    gaming_page = [
        ProductCard(
            spu_id=index,
            sku_id=index,
            title=f"Gaming Mouse {index}",
            brand="Other",
            category="mouse",
            price="99.00",
            stock=1,
            specs={"usage": "gaming"},
        )
        for index in range(1, 151)
    ]
    office_candidate = ProductCard(
        spu_id=151,
        sku_id=151,
        title="Office Mouse",
        brand="Other",
        category="mouse",
        price="109.00",
        stock=1,
        specs={"usage": "office"},
    )

    class PagingRepository(catalog_repository.CatalogRepository):
        def __init__(self) -> None:
            super().__init__(None)  # type: ignore[arg-type]
            self.calls: list[tuple[int, int]] = []

        async def _fetch_candidate_page(self, request, *, offset: int, limit: int):
            self.calls.append((offset, limit))
            if offset == 0:
                return gaming_page, False
            return [office_candidate], True

    repository = PagingRepository()
    products = await repository.search_products(
        ProductSearchRequest(
            query="mouse",
            excluded_usage=["gaming"],
            limit=3,
        )
    )

    assert [product.sku_id for product in products] == [151]
    assert repository.calls == [(0, 150), (150, 150)]


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
