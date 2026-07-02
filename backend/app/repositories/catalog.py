from decimal import Decimal

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Brand, Category, Sku, Spu
from app.schemas.catalog import ProductCard, ProductSearchRequest


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
            .limit(max(request.limit * 3, request.limit))
        )

        if request.query:
            like = f"%{request.query}%"
            stmt = stmt.where(
                or_(Sku.title.ilike(like), Spu.title.ilike(like), Brand.name.ilike(like))
            )

        if request.category:
            stmt = stmt.where(Category.name.ilike(f"%{request.category}%"))

        if request.min_price is not None:
            stmt = stmt.where(Sku.price >= request.min_price)
        if request.max_price is not None:
            stmt = stmt.where(Sku.price <= request.max_price)

        rows = (await self.session.execute(stmt)).all()
        products: list[ProductCard] = []
        for sku, spu, brand, category in rows:
            specs = {str(k): str(v) for k, v in (sku.specs_json or {}).items()}
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
                    specs=specs,
                    image_url=sku.image_url,
                )
            )
            if len(products) >= request.limit:
                break

        return products


def _matches_filters(specs: dict[str, str], filters: dict[str, str]) -> bool:
    normalized_specs = {key.lower(): value.lower() for key, value in specs.items()}
    for key, expected in filters.items():
        actual = normalized_specs.get(key.lower())
        if actual is None or expected.lower() not in actual:
            return False
    return True
