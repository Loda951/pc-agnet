import re
from decimal import Decimal

from app.schemas.catalog import ProductSearchRequest
from app.schemas.chat import BoundaryClassification

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

AFTER_SALES_TERMS = ["退货", "换货", "退款", "维修", "售后", "工单", "保修", "赔付"]
AFTER_SALES_INFO_TERMS = ["政策", "规则", "流程", "说明", "多久", "条件", "材料", "怎么", "如何"]
AFTER_SALES_WRITE_TERMS = [
    "创建",
    "申请",
    "提交",
    "办理",
    "发起",
    "开",
    "帮我",
    "我要",
    "想退",
    "想换",
]
ORDER_WRITE_TERMS = [
    "取消订单",
    "修改订单",
    "改地址",
    "改收货",
    "换地址",
    "催发货",
    "补发",
    "下单",
    "支付",
]
OUT_OF_SCOPE_TERMS = [
    "天气",
    "新闻",
    "股票",
    "基金",
    "医疗",
    "法律",
    "旅游",
    "菜谱",
    "外卖",
    "电影",
    "写代码",
    "python",
    "javascript",
    "论文",
    "作文",
    "手机",
    "汽车",
    "衣服",
]
IN_SCOPE_READ_ONLY_TERMS = [
    "订单",
    "物流",
    "快递",
    "发货",
    "推荐",
    "预算",
    "对比",
    "库存",
    "价格",
    "参数",
    "规格",
    "无线",
    "有线",
    "rgb",
    "红轴",
    "青轴",
    "外设",
    "pc",
    "电脑",
    "客服",
    "你好",
    "您好",
]


def classify_boundary(message: str) -> BoundaryClassification:
    lowered = message.lower()
    compact = re.sub(r"\s+", "", lowered)

    if _requires_human_handoff(message, compact):
        return BoundaryClassification(
            classification="human_handoff_required",
            reason="涉及售后、订单变更或其他需要人工确认的写操作",
            display_message=(
                "这个请求需要人工客服确认后处理。请补充订单号、商品明细、诉求类型和问题描述，"
                "我会按人工接管入口整理信息。"
            ),
        )

    if _is_in_scope_read_only(message, lowered, compact):
        return BoundaryClassification(
            classification="in_scope_auto",
            reason="属于 PC 外设商城 read-only 咨询或查询范围",
            display_message="可自动回答",
        )

    return BoundaryClassification(
        classification="out_of_scope",
        reason="不属于 PC 外设商城客服的 read-only 服务范围",
        display_message=(
            "这个问题超出 PC 外设商城客服范围。我可以继续帮你做外设推荐、订单物流查询，"
            "或说明售后政策。"
        ),
    )


def _requires_human_handoff(message: str, compact: str) -> bool:
    if any(term in compact for term in ORDER_WRITE_TERMS):
        return True

    has_after_sales = any(term in message for term in AFTER_SALES_TERMS)
    if not has_after_sales:
        return False

    asks_for_policy = any(term in message for term in AFTER_SALES_INFO_TERMS)
    asks_for_write = any(term in message for term in AFTER_SALES_WRITE_TERMS)
    explicit_after_sales_action = re.search(
        r"(退货|换货|退款|维修|售后).{0,8}(申请|办理|处理|安排|提交|创建|开)",
        message,
    )
    user_wants_after_sales_action = re.search(
        r"(我要|帮我|给我|想要|需要).{0,8}(退货|换货|退款|维修|售后|工单)",
        message,
    )
    bare_after_sales_action = any(term in compact for term in ["退货", "换货", "退款", "维修"])

    return bool(
        explicit_after_sales_action
        or user_wants_after_sales_action
        or asks_for_write
        or (bare_after_sales_action and not asks_for_policy)
    )


def _is_in_scope_read_only(message: str, lowered: str, compact: str) -> bool:
    if any(term in compact for term in OUT_OF_SCOPE_TERMS) and not _has_strong_scope_signal(
        message, lowered, compact
    ):
        return False

    if _has_strong_scope_signal(message, lowered, compact):
        return True

    return any(term in compact for term in IN_SCOPE_READ_ONLY_TERMS)


def _has_strong_scope_signal(message: str, lowered: str, compact: str) -> bool:
    return (
        any(keyword in lowered or keyword in message for keyword in CATEGORY_KEYWORDS)
        or any(term in compact for term in ["订单", "物流", "快递", "发货"])
        or any(term in message for term in AFTER_SALES_TERMS)
        or any(term in compact for term in ["外设", "pc", "电脑"])
    )


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
