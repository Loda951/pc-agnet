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
from app.schemas.catalog import ProductCard, ProductSearchRequest, ProductSpecCondition

CATEGORY_ALIASES = {
    "mouse": "鼠标",
    "mice": "鼠标",
    "鼠标": "鼠标",
    "游戏鼠标": "鼠标",
    "keyboard": "键盘",
    "keyboards": "键盘",
    "键盘": "键盘",
    "机械键盘": "键盘",
    "headphone": "耳机",
    "headphones": "耳机",
    "headset": "耳机",
    "headsets": "耳机",
    "earphone": "耳机",
    "earphones": "耳机",
    "耳机": "耳机",
    "耳麦": "耳机",
    "头戴耳机": "耳机",
    "游戏耳机": "耳机",
    "monitor": "显示器",
    "monitors": "显示器",
    "display": "显示器",
    "screen": "显示器",
    "显示器": "显示器",
    "屏幕": "显示器",
    "speaker": "音箱",
    "speakers": "音箱",
    "音箱": "音箱",
    "音响": "音箱",
    "蓝牙音箱": "音箱",
    "webcam": "摄像头",
    "webcams": "摄像头",
    "camera": "摄像头",
    "摄像头": "摄像头",
    "网络摄像头": "摄像头",
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
    "是什么",
    "pc",
    "rgb",
    "sku",
    "spu",
    "sale",
    "sales",
    "rank",
    "ranking",
    "top",
}

TRUE_VALUES = {"是", "true", "yes", "1", "有", "支持", "wireless"}
FALSE_VALUES = {"否", "false", "no", "0", "无", "不支持", "wired"}
DB_VALUE_ALIASES = {
    "connection_type": {
        "wireless": {
            "wireless",
            "wifi",
            "wi-fi",
            "bluetooth",
            "bt",
            "无线",
            "蓝牙",
            "无线蓝牙",
            "2.4g",
            "2.4g 无线",
            "三模",
        },
        "bluetooth": {"wireless", "bluetooth", "bt", "无线", "蓝牙", "无线蓝牙"},
        "wired": {"wired", "usb", "usb-a", "usb-c", "cable", "有线", "有线连接"},
    },
    "wireless": {
        "true": {"true", "yes", "1", "是", "有", "支持", "wireless", "bluetooth", "无线", "蓝牙"},
        "false": {"false", "no", "0", "否", "无", "不支持", "wired", "有线"},
    },
    "microphone": {
        "yes": {"yes", "true", "1", "是", "有", "带", "支持", "带麦", "麦克风"},
        "no": {"no", "false", "0", "否", "无", "不带", "不支持"},
    },
    "backlit": {
        "yes": {"yes", "true", "1", "是", "有", "带", "支持", "rgb", "背光", "灯光", "白光"},
        "no": {"no", "false", "0", "无", "无背光", "不带", "不支持"},
    },
    "switches": {
        "red": {"red", "red switch", "red switches", "红", "红轴", "线性红轴", "静音红轴"},
        "blue": {"blue", "blue switch", "blue switches", "青", "青轴"},
        "brown": {"brown", "brown switch", "brown switches", "茶", "茶轴", "段落茶轴"},
        "magnetic": {"magnetic", "magnetic switch", "magnetic switches", "磁", "磁轴"},
    },
    "color": {
        "black": {"black", "黑", "黑色"},
        "white": {"white", "白", "白色"},
        "silver": {"silver", "银", "银色"},
        "gray": {"gray", "grey", "灰", "灰色"},
        "pink": {"pink", "粉", "粉色"},
    },
    "resolution": {
        "2560x1440": {"2560x1440", "2k", "1440p"},
        "4k": {"4k", "3840x2160"},
        "1080p": {"1080p", "1080p hdr", "1920x1080", "full hd", "fhd"},
    },
    "refresh_rate": {
        "144hz": {"144hz", "144 hz", "144赫兹"},
        "165hz": {"165hz", "165 hz", "165赫兹"},
        "240hz": {"240hz", "240 hz", "240赫兹"},
        "75hz": {"75hz", "75 hz", "75赫兹"},
    },
}
MAX_CANDIDATE_PAGES = 50


class CatalogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search_products(self, request: ProductSearchRequest) -> list[ProductCard]:
        query_tokens = _query_tokens(request.query)
        page_size = _candidate_page_size(request.limit)
        candidate_target = (
            min(page_size, max(request.limit * 10, 50))
            if request.usage_preferred_conditions
            else request.limit
        )
        eligible_products: list[ProductCard] = []
        offset = 0
        for _ in range(MAX_CANDIDATE_PAGES):
            page, exhausted = await self._fetch_candidate_page(
                request,
                offset=offset,
                limit=page_size,
            )
            eligible_products.extend(
                _take_eligible_products(
                    page,
                    excluded_usage=request.excluded_usage,
                    usage_scenario=request.usage_scenario,
                    required_conditions=request.usage_required_conditions,
                    limit=page_size,
                )
            )
            if len(eligible_products) >= candidate_target or exhausted:
                break
            offset += page_size

        if request.sort == "price_desc":
            eligible_products.sort(
                key=lambda product: (
                    -product.price,
                    0 if product.stock > 0 else 1,
                    -product.sku_sales_count,
                    product.sku_id,
                )
            )
        elif request.sort == "price_asc":
            eligible_products.sort(
                key=lambda product: (
                    product.price,
                    0 if product.stock > 0 else 1,
                    -product.sku_sales_count,
                    product.sku_id,
                )
            )
        elif request.sort == "sales":
            eligible_products.sort(
                key=lambda product: (
                    -product.sku_sales_count,
                    0 if product.stock > 0 else 1,
                    product.price,
                    product.sku_id,
                )
            )
        elif request.sort == "stock":
            eligible_products.sort(
                key=lambda product: (
                    -product.stock,
                    -product.sku_sales_count,
                    product.price,
                    product.sku_id,
                )
            )
        else:
            ranked_products = [
                (_score_product(product, request, query_tokens), product)
                for product in eligible_products
            ]
            ranked_products.sort(
                key=lambda item: (
                    -item[0],
                    -item[1].sku_sales_count,
                    0 if item[1].stock > 0 else 1,
                    item[1].price,
                    item[1].title,
                )
            )
            eligible_products = [product for _, product in ranked_products]
        return eligible_products[: request.limit]

    async def search_product_series_by_sales(
        self,
        request: ProductSearchRequest,
    ) -> list[ProductCard]:
        """Return one representative SKU per SPU, ordered by aggregate SPU sales."""
        page_size = _candidate_page_size(request.limit)
        eligible_products: list[ProductCard] = []
        offset = 0
        for _ in range(MAX_CANDIDATE_PAGES):
            page, exhausted = await self._fetch_candidate_page(
                request,
                offset=offset,
                limit=page_size,
            )
            eligible_products.extend(
                _take_eligible_products(
                    page,
                    excluded_usage=request.excluded_usage,
                    usage_scenario=request.usage_scenario,
                    required_conditions=request.usage_required_conditions,
                    limit=page_size,
                )
            )
            if exhausted:
                break
            offset += page_size

        representatives: dict[int, ProductCard] = {}
        for product in eligible_products:
            current = representatives.get(product.spu_id)
            if current is None or _series_representative_key(product) < (
                _series_representative_key(current)
            ):
                representatives[product.spu_id] = product
        ranked = sorted(
            representatives.values(),
            key=lambda product: (
                -product.sales_count,
                product.spu_id,
            ),
        )
        return ranked[: request.limit]

    async def list_facets(
        self,
        *,
        facet: str,
        category: str | None = None,
        brand: str | None = None,
        spec_key: str | None = None,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        filters: dict[str, str] | None = None,
        limit: int = 20,
    ) -> list[tuple[str, int]]:
        rows = await self._facet_candidate_rows(category, brand, min_price, max_price)
        attributes_by_sku = await self._load_attributes([sku.id for sku, *_ in rows])
        counts: dict[str, int] = {}
        normalized_spec_key = spec_key.lower() if spec_key else None
        for sku, _spu, row_brand, row_category in rows:
            specs = _merge_specs(sku.specs_json or {}, attributes_by_sku.get(sku.id, {}))
            if filters and not _matches_filters(specs, filters):
                continue
            if facet == "brand":
                values = [row_brand.name]
            elif facet == "category":
                values = [row_category.name]
            elif facet == "spec_key":
                values = list(specs)
            elif facet == "spec_value":
                if normalized_spec_key:
                    values = [
                        value for key, value in specs.items() if key.lower() == normalized_spec_key
                    ]
                else:
                    values = list(specs.values())
            else:
                raise ValueError(f"unsupported catalog facet: {facet}")
            for value in values:
                value = str(value).strip()
                if not value:
                    continue
                counts[value] = counts.get(value, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]

    async def _facet_candidate_rows(
        self,
        category: str | None,
        brand: str | None,
        min_price: Decimal | None,
        max_price: Decimal | None,
    ):
        stmt: Select = (
            select(Sku, Spu, Brand, Category)
            .join(Spu, Sku.spu_id == Spu.id)
            .join(Brand, Spu.brand_id == Brand.id)
            .join(Category, Spu.category_id == Category.id)
            .where(Sku.status == 1, Spu.status == 1)
        )
        if category_terms := _category_terms(category):
            stmt = stmt.where(or_(*(Category.name.ilike(f"%{term}%") for term in category_terms)))
        if brand:
            stmt = stmt.where(Brand.name.ilike(f"%{brand}%"))
        if min_price is not None:
            stmt = stmt.where(Sku.price >= min_price)
        if max_price is not None:
            stmt = stmt.where(Sku.price <= max_price)
        return (await self.session.execute(stmt)).all()

    async def _fetch_candidate_page(
        self,
        request: ProductSearchRequest,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[ProductCard], bool]:
        stmt = _catalog_search_statement(request, limit=limit, offset=offset)
        rows = (await self.session.execute(stmt)).all()
        attributes_by_sku = await self._load_attributes([sku.id for sku, *_ in rows])
        products: list[ProductCard] = []
        for sku, spu, brand, category in rows:
            specs = _merge_specs(sku.specs_json or {}, attributes_by_sku.get(sku.id, {}))
            if request.filters and not _matches_filters(specs, request.filters):
                continue
            products.append(
                ProductCard(
                    spu_id=spu.id,
                    sku_id=sku.id,
                    title=sku.title,
                    brand=brand.name,
                    category=category.name,
                    price=Decimal(sku.price),
                    stock=sku.stock,
                    sku_sales_count=sku.sales_count,
                    sales_count=spu.sales_count,
                    specs=specs,
                    image_url=sku.image_url,
                )
            )
        return products, len(rows) < limit

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


def _candidate_page_size(limit: int) -> int:
    return min(max(limit * 50, 100), 1000)


def _series_representative_key(product: ProductCard) -> tuple[int, int, Decimal, int]:
    return (
        -product.sku_sales_count,
        0 if product.stock > 0 else 1,
        product.price,
        product.sku_id,
    )


def _catalog_search_statement(
    request: ProductSearchRequest,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> Select:
    page_limit = limit or _candidate_page_size(request.limit)
    if request.sort == "price_desc":
        order_by = (Sku.price.desc(), Sku.stock.desc(), Sku.sales_count.desc(), Sku.id.asc())
    elif request.sort == "price_asc":
        order_by = (Sku.price.asc(), Sku.stock.desc(), Sku.sales_count.desc(), Sku.id.asc())
    elif request.sort == "stock":
        order_by = (Sku.stock.desc(), Sku.sales_count.desc(), Sku.price.asc(), Sku.id.asc())
    else:
        order_by = (Sku.sales_count.desc(), Sku.stock.desc(), Sku.price.asc(), Sku.id.asc())
    stmt: Select = (
        select(Sku, Spu, Brand, Category)
        .join(Spu, Sku.spu_id == Spu.id)
        .join(Brand, Spu.brand_id == Brand.id)
        .join(Category, Spu.category_id == Category.id)
        .where(Sku.status == 1, Spu.status == 1)
        .order_by(*order_by)
        .limit(page_limit)
        .offset(offset)
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
        stmt = stmt.where(or_(*(Category.name.ilike(f"%{term}%") for term in category_terms)))
    if request.min_price is not None:
        stmt = stmt.where(Sku.price >= request.min_price)
    if request.max_price is not None:
        stmt = stmt.where(Sku.price <= request.max_price)
    for brand in request.excluded_brands:
        stmt = stmt.where(~Brand.name.ilike(f"%{brand}%"))
    return stmt


def _take_eligible_products(
    products: list[ProductCard],
    *,
    excluded_usage: list[str],
    limit: int,
    usage_scenario: str | None = None,
    required_conditions: list[ProductSpecCondition] | None = None,
) -> list[ProductCard]:
    required_conditions = required_conditions or []
    if not excluded_usage and not usage_scenario and not required_conditions:
        return products[:limit]
    excluded_usage_terms = {term for usage in excluded_usage for term in _usage_terms(usage)}
    required_usage_terms = _usage_terms(usage_scenario) if usage_scenario else set()
    eligible: list[ProductCard] = []
    for product in products:
        if not all(
            _matches_spec_condition(product.specs, condition) for condition in required_conditions
        ):
            continue
        haystack = " ".join([product.title, product.category, *product.specs.values()]).lower()
        if required_usage_terms and not any(term in haystack for term in required_usage_terms):
            continue
        if any(term in haystack for term in excluded_usage_terms):
            continue
        eligible.append(product)
        if len(eligible) >= limit:
            break
    return eligible


def _usage_terms(usage: str) -> set[str]:
    normalized = usage.strip().lower()
    aliases = {
        "gaming": {"gaming", "游戏", "电竞"},
        "office": {"office", "办公"},
        "video_meeting": {"video_meeting", "video meeting", "视频会议", "开会", "网课"},
        "live_streaming": {"live_streaming", "live streaming", "直播", "主播"},
    }
    return aliases.get(normalized, {normalized})


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
    for condition in request.usage_preferred_conditions:
        if _matches_spec_condition(product.specs, condition):
            score += 6
    if product.stock > 0:
        score += 1
    return score


def _matches_filters(specs: dict[str, str], filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        if not _matches_single_filter(specs, key, expected):
            return False
    return True


def _matches_spec_condition(specs: dict[str, str], condition: ProductSpecCondition) -> bool:
    if condition.operator == "exact":
        actual = next(
            (value for key, value in specs.items() if key.lower() == condition.key.lower()),
            None,
        )
        return actual is not None and actual.strip().lower() == condition.values[0].strip().lower()
    if condition.operator == "eq":
        return _matches_single_filter(specs, condition.key, condition.values[0])
    if condition.operator == "in":
        actual = next(
            (value for key, value in specs.items() if key.lower() == condition.key.lower()),
            None,
        )
        allowed = {value.strip().lower() for value in condition.values}
        return actual is not None and actual.strip().lower() in allowed

    actual = next(
        (value for key, value in specs.items() if key.lower() == condition.key.lower()),
        None,
    )
    if actual is None:
        return False
    actual_number = _numeric_value(actual)
    expected_number = _numeric_value(condition.values[0])
    if actual_number is None or expected_number is None:
        return False
    if condition.operator == "gte":
        return actual_number >= expected_number
    return actual_number <= expected_number


def _numeric_value(value: str) -> Decimal | None:
    if match := re.search(r"-?\d+(?:\.\d+)?", value.replace(",", "")):
        return Decimal(match.group(0))
    return None


def _matches_single_filter(specs: dict[str, str], key: str, expected: str) -> bool:
    normalized_specs = {item_key.lower(): value.lower() for item_key, value in specs.items()}
    key = key.lower()
    expected_lower = expected.lower()

    if key in {"connection_type", "wireless"} and expected_lower in {
        "wireless",
        "bluetooth",
        "无线",
        "蓝牙",
        "是",
        "true",
        "yes",
    }:
        return _is_wireless_match(normalized_specs)
    if key in {"connection_type", "wireless"} and expected_lower in {
        "wired",
        "有线",
        "否",
        "false",
        "no",
    }:
        return _is_wired_match(normalized_specs)

    actual = normalized_specs.get(key)
    if actual is None:
        return False
    return _value_matches_aliases(key, actual, expected_lower)


def _value_matches_aliases(key: str, actual: str, expected: str) -> bool:
    expected_aliases = _db_value_aliases(key, expected)
    actual_aliases = _db_value_aliases(key, actual)
    if expected_aliases & actual_aliases:
        return True
    return any(alias in actual for alias in expected_aliases)


def _db_value_aliases(key: str, value: str) -> set[str]:
    normalized = value.strip().lower()
    compact = normalized.replace(" ", "")
    aliases = {normalized, compact}
    for canonical, values in DB_VALUE_ALIASES.get(key, {}).items():
        lowered_values = {item.lower() for item in values}
        compact_values = {item.replace(" ", "") for item in lowered_values}
        if normalized == canonical or normalized in lowered_values or compact in compact_values:
            aliases.add(canonical)
            aliases.update(lowered_values)
            aliases.update(compact_values)
            break
    return aliases


def _is_wireless_match(specs: dict[str, str]) -> bool:
    connection = specs.get("connection_type", "")
    wireless = specs.get("wireless", "")
    return _value_matches_aliases("connection_type", connection, "wireless") or (
        _value_matches_aliases("wireless", wireless, "true")
    )


def _is_wired_match(specs: dict[str, str]) -> bool:
    connection = specs.get("connection_type", "")
    wireless = specs.get("wireless", "")
    return _value_matches_aliases("connection_type", connection, "wired") or (
        _value_matches_aliases("wireless", wireless, "false")
    )
