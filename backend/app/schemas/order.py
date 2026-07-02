from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class OrderItemCard(BaseModel):
    id: int
    sku_id: int
    sku_name: str
    sku_specs: dict | None
    price: Decimal
    quantity: int


class LogisticsCard(BaseModel):
    express_company: str | None
    logistic_no: str | None
    status: int
    trace: list[dict] = Field(default_factory=list)


class OrderCard(BaseModel):
    id: int
    status: int
    status_label: str
    pay_amount: Decimal
    created_at: datetime
    items: list[OrderItemCard]
    logistics: LogisticsCard | None
