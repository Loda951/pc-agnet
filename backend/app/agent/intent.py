import re
from decimal import Decimal

from app.schemas.catalog import ProductSearchRequest

CATEGORY_KEYWORDS = {
    "鼠标": "鼠标",
    "mouse": "鼠标",
    "键盘": "键盘",
    "keyboard": "键盘",
    "耳机": "耳机",
    "headphone": "耳机",
    "headset": "耳机",
    "显示器": "显示器",
    "monitor": "显示器",
    "摄像头": "摄像头",
    "webcam": "摄像头",
}


def classify_intent(message: str) -> str:
    lowered = message.lower()
    if any(keyword in message for keyword in ["退货", "换货", "退款", "维修", "售后", "工单"]):
        return "after_sales"
    if any(keyword in message for keyword in ["订单", "物流", "快递", "发货"]) or re.search(
        r"\b\d{8,}\b", lowered
    ):
        return "order_status"
    if any(keyword in lowered for keyword in CATEGORY_KEYWORDS) or any(
        keyword in message for keyword in ["推荐", "预算", "买", "选", "对比"]
    ):
        return "product_recommendation"
    return "general"


def extract_order_id(message: str) -> int | None:
    match = re.search(r"\b(\d{8,})\b", message)
    if match:
        return int(match.group(1))
    return None


def build_product_search(message: str) -> ProductSearchRequest:
    lowered = message.lower()
    category = None
    for keyword, mapped in CATEGORY_KEYWORDS.items():
        if keyword in lowered or keyword in message:
            category = mapped
            break

    max_price = None
    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|以内|以下|预算)", message)
    if budget_match:
        max_price = Decimal(budget_match.group(1))

    filters: dict[str, str] = {}
    if "无线" in message or "wireless" in lowered:
        filters["connection_type"] = "Wireless"
    elif "有线" in message or "wired" in lowered:
        filters["connection_type"] = "Wired"
    if "rgb" in lowered:
        filters["backlit"] = "RGB"
    if "红轴" in message:
        filters["switches"] = "Red"
    if "青轴" in message:
        filters["switches"] = "Blue"

    query = message
    for word in ["推荐", "预算", "以内", "以下", "我想买", "买", "选"]:
        query = query.replace(word, " ")
    query = re.sub(r"\d+(?:\.\d+)?\s*(元|块)?", " ", query).strip()

    return ProductSearchRequest(
        query=query if len(query) > 1 else "",
        category=category,
        max_price=max_price,
        filters=filters,
        limit=6,
    )
