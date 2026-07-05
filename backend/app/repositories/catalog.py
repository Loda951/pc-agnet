import re
from decimal import Decimal

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttributeKey,
    AttributeValue,
    Brand,
    Category,
    GoodsAttributeRelation,
    Sku,
    Spu,
)
from app.schemas.catalog import ProductCard, ProductSearchRequest

CATEGORY_ALIASES = {
    "mouse": "鼠标",
    "mice": "鼠标",
    "鼠标": "鼠标",
    "keyboard": "键盘",
    "keyboards": "键盘",
    "键盘": "键盘",
    "headphone": "耳机",
    "headphones": "耳机",
    "headset": "耳机",
    "耳机": "耳机",
    "monitor": "显示器",
    "monitors": "显示器",
    "显示器": "显示器",
    "speaker": "音箱",
    "speakers": "音箱",
    "音箱": "音箱",
    "webcam": "摄像头",
    "webcams": "摄像头",
    "摄像头": "摄像头",
}

QUERY_STOP_WORDS = {
    "推荐",
    "预算",
    "以内",
    "以下",
    "帮我",
    "我想",
    "想买",
    "买",
    "选",
    "怎么",
    "对比",
    "比较",
    "哪款",
    "哪个",
    "无线",
    "有线",
    "鼠标",
    "键盘",
    "耳机",
    "显示器",
    "音箱",
    "摄像头",
    "外设",
    "pc",
    "rgb",
}

TRUE_VALUES = {"是", "true", "yes", "1", "有", "支持", "wireless"}
FALSE_VALUES = {"否", "false", "no", "0", "无", "不支持", "wired"}


class CatalogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search_products(self, request: ProductSearchRequest) -> list[ProductCard]:
        stmt: Select = (
            select(Sku, Spu, Brand, Category)
            .join(Spu, Sku.spu_id == Spu.id)
            .join(Brand, Spu.brand_id == Brand.id)
            .join(Category, Spu.category_id == Category.id)
            .where(Sku.status == 1, Spu.status == 1)
            .order_by(Sku.stock.desc(), Sku.price.asc())
            .limit(_candidate_limit(request.limit))
        )
        query_tokens = _query_tokens(request.query)

        if query_tokens:
            conditions = []
            for token in query_tokens:
                like = f"%{token}%"
                conditions.extend(
                    [
                        Sku.title.ilike(like),
                        Spu.title.ilike(like),
                        Brand.name.ilike(like),
                        Category.name.ilike(like),
                    ]
                )
            stmt = stmt.where(or_(*conditions))

        if category_terms := _category_terms(request.category):
            stmt = stmt.where(
                or_(*(Category.name.ilike(f"%{term}%") for term in category_terms))
            )

        if request.min_price is not None:
            stmt = stmt.where(Sku.price >= request.min_price)
        if request.max_price is not None:
            stmt = stmt.where(Sku.price <= request.max_price)

        rows = (await self.session.execute(stmt)).all()
        attributes_by_sku = await self._load_attributes([sku.id for sku, *_ in rows])
        ranked_products: list[tuple[int, ProductCard]] = []
        for sku, spu, brand, category in rows:
            specs = _merge_specs(sku.specs_json or {}, attributes_by_sku.get(sku.id, {}))
            if request.filters and not _matches_filters(specs, request.filters):
                continue
            product = ProductCard(
                spu_id=spu.id,
                sku_id=sku.id,
                title=sku.title,
                brand=brand.name,
                category=category.name,
                price=Decimal(sku.price),
                stock=sku.stock,
                specs=specs,
                image_url=sku.image_url,
            )
            ranked_products.append(
                (
                    _score_product(product, request, query_tokens),
                    product,
                )
            )
        ranked_products.sort(
            key=lambda item: (
                -item[0],
                0 if item[1].stock > 0 else 1,
                item[1].price,
                item[1].title,
            )
        )
        return [product for _, product in ranked_products[: request.limit]]

    async def _load_attributes(self, sku_ids: list[int]) -> dict[int, dict[str, str]]:
        if not sku_ids:
            return {}
        stmt = (
            select(GoodsAttributeRelation.sku_id, AttributeKey.name, AttributeValue.value)
            .join(AttributeKey, GoodsAttributeRelation.attr_key_id == AttributeKey.id)
            .join(AttributeValue, GoodsAttributeRelation.attr_value_id == AttributeValue.id)
            .where(GoodsAttributeRelation.sku_id.in_(sku_ids))
        )
        attributes: dict[int, dict[str, str]] = {}
        for sku_id, name, value in (await self.session.execute(stmt)).all():
            attributes.setdefault(sku_id, {})[str(name)] = str(value)
        return attributes


def _candidate_limit(limit: int) -> int:
    return min(max(limit * 50, 100), 1000)


def _category_terms(category: str | None) -> set[str]:
    if not category:
        return set()
    lowered = category.lower()
    mapped = CATEGORY_ALIASES.get(lowered, category)
    return {category, mapped}


def _query_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*|[\u4e00-\u9fff]+", query.lower())
    return [token for token in tokens if not _is_noise_token(token)]


def _is_noise_token(token: str) -> bool:
    if token in QUERY_STOP_WORDS:
        return True
    if any("\u4e00" <= char <= "\u9fff" for char in token):
        return any(stop_word in token for stop_word in QUERY_STOP_WORDS)
    return False


def _merge_specs(specs_json: dict, attributes: dict[str, str]) -> dict[str, str]:
    specs = {str(key): str(value) for key, value in specs_json.items()}
    for key, value in attributes.items():
        specs.setdefault(key, value)
    return specs


def _score_product(
    product: ProductCard,
    request: ProductSearchRequest,
    query_tokens: list[str],
) -> int:
    title = product.title.lower()
    brand = product.brand.lower()
    category = product.category.lower()
    spec_text = " ".join(product.specs.values()).lower()
    score = 0

    for token in query_tokens:
        if token in title:
            score += 8
        if token in brand:
            score += 5
        if token in category:
            score += 3
        if token in spec_text:
            score += 2

    if request.category:
        score += 3
    for key, expected in request.filters.items():
        if _matches_single_filter(product.specs, key, expected):
            score += 4
    if product.stock > 0:
        score += 1
    return score


def _matches_filters(specs: dict[str, str], filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        if not _matches_single_filter(specs, key, expected):
            return False
    return True


def _matches_single_filter(specs: dict[str, str], key: str, expected: str) -> bool:
    normalized_specs = {item_key.lower(): value.lower() for item_key, value in specs.items()}
    key = key.lower()
    expected_lower = expected.lower()

    if key in {"connection_type", "wireless"} and expected_lower in {"wireless", "无线", "是"}:
        return _is_wireless_match(normalized_specs)
    if key in {"connection_type", "wireless"} and expected_lower in {"wired", "有线", "否"}:
        return _is_wired_match(normalized_specs)

    actual = normalized_specs.get(key)
    if actual is None:
        return False
    return expected_lower in actual


def _is_wireless_match(specs: dict[str, str]) -> bool:
    connection = specs.get("connection_type", "")
    wireless = specs.get("wireless", "")
    return "wireless" in connection or wireless in TRUE_VALUES


def _is_wired_match(specs: dict[str, str]) -> bool:
    connection = specs.get("connection_type", "")
    wireless = specs.get("wireless", "")
    return "wired" in connection or wireless in FALSE_VALUES
