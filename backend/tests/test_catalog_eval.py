
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.catalog import _catalog_search_statement
from app.schemas.catalog import ProductSearchRequest
from app.tools.catalog import (
    ProductQueryPlan,
    RuleBasedCatalogQueryPlanner,
    _facet_query_plan,
    _plan_to_product_search,
    validate_product_query_plan,
)
from app.tools.schemas import CatalogFacetInput, CatalogSearchInput


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


PLANNER_GOLDEN_CASES = [
    (
        "wireless mouse under 300 from Logitech",
        {
            "category": "mouse",
            "brands": ["Logitech"],
            "max_price": Decimal("300"),
            "filters": {"connection_type": "Wireless"},
        },
    ),
    (
        "144Hz 2K monitor",
        {
            "category": "monitor",
            "filters": {"refresh_rate": "144Hz", "resolution": "2560x1440"},
        },
    ),
    (
        "gaming keyboard with red switches",
        {
            "category": "keyboard",
            "filters": {"switches": "Red"},
            "usage_scenario": "gaming",
        },
    ),
    (
        "wireless headset with microphone",
        {
            "category": "headset",
            "filters": {"connection_type": "Wireless", "microphone": "Yes"},
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("query", "expected"), PLANNER_GOLDEN_CASES)
async def test_catalog_rule_planner_golden_cases(query: str, expected: dict) -> None:
    raw_plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query, limit=3)
    )
    plan = validate_product_query_plan(raw_plan)

    for key, expected_value in expected.items():
        actual = getattr(plan, key)
        if key == "filters":
            for filter_key, filter_value in expected_value.items():
                assert actual[filter_key] == filter_value
        else:
            assert actual == expected_value


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "what mouse brands do you sell",
            {"facet": "brand", "category": "mouse", "supported": True},
        ),
        (
            "what peripheral categories does Razer sell",
            {"facet": "category", "brand": "Razer", "supported": True},
        ),
        (
            "what keyboard switches are available",
            {
                "facet": "spec_value",
                "category": "keyboard",
                "spec_key": "switches",
                "supported": True,
            },
        ),
        (
            "what monitor switches are available",
            {
                "facet": "spec_value",
                "category": "monitor",
                "spec_key": "switches",
                "supported": False,
                "unsupported_reason": "unsupported spec_key for monitor",
            },
        ),
    ],
)
def test_catalog_facet_planner_golden_cases(query: str, expected: dict) -> None:
    plan = _facet_query_plan(CatalogFacetInput(query=query))

    for key, expected_value in expected.items():
        actual = getattr(plan, key)
        if key == "unsupported_reason":
            assert expected_value in actual
        else:
            assert actual == expected_value


def test_catalog_product_plan_to_search_request_preserves_structured_filters() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="recommend a wireless Logitech mouse under 300",
            category="mouse",
            brands=["Logitech"],
            max_price=Decimal("300"),
            filters={"connection_type": "Wireless"},
            planner="llm",
            limit=3,
        )
    )

    request = _plan_to_product_search(plan)

    assert request.category == "mouse"
    assert request.max_price == Decimal("300")
    assert request.filters == {"connection_type": "Wireless"}
    assert request.limit == 3
    assert "Logitech" in request.query
    assert "recommend" not in request.query.lower()


def test_catalog_product_plan_rejects_sql_injection_like_filter_key() -> None:
    with pytest.raises(ValueError, match="unsupported catalog filters"):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find mouse",
                category="mouse",
                filters={"sku.price); drop table sku; --": "1"},
            )
        )


def test_catalog_search_sql_contains_expected_joins_filters_and_ordering() -> None:
    request = ProductSearchRequest(
        query="Logitech",
        category="mouse",
        min_price=Decimal("100"),
        max_price=Decimal("300"),
        excluded_brands=["Razer"],
        limit=3,
    )

    compiled = _sql(_catalog_search_statement(request, limit=3, offset=0))

    assert "join spu" in compiled
    assert "join brand" in compiled
    assert "join category" in compiled
    assert "sku.status = 1" in compiled
    assert "spu.status = 1" in compiled
    assert "sku.price >= 100" in compiled
    assert "sku.price <= 300" in compiled
    assert "brand.name not ilike" in compiled
    assert "category.name ilike" in compiled
    assert "order by sku.sales_count desc, sku.stock desc, sku.price asc" in compiled
    assert "limit 3" in compiled


def test_catalog_search_sql_does_not_embed_post_filter_specs_as_raw_sql() -> None:
    request = ProductSearchRequest(
        query="",
        category="monitor",
        filters={"refresh_rate": "144Hz", "resolution": "2560x1440"},
        limit=5,
    )

    compiled = _sql(_catalog_search_statement(request, limit=5, offset=0))

    assert "144hz" not in compiled
    assert "2560x1440" not in compiled
    assert "goods_attribute_relation" not in compiled
    assert "limit 5" in compiled
