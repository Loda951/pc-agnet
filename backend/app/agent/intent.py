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
ORDER_CHANGE_WRITE_TERMS = [
    "取消订单",
    "修改订单",
    "改地址",
    "改收货",
    "换地址",
    "催发货",
    "补发",
]
PURCHASE_ACTION_TERMS = [
    "下单",
    "支付",
    "付款",
    "提交订单",
    "结算",
]
PURCHASE_INFO_TERMS = [
    "怎么",
    "如何",
    "流程",
    "步骤",
    "方式",
    "支持",
    "入口",
    "哪里",
    "在哪",
    "说明",
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

BOUNDARY_MESSAGES = {
    "in_scope_auto": {
        "reason": "属于 PC 外设商城客服范围，优先进入自动应答流程",
        "display_message": "可自动回答",
    },
    "human_handoff_required": {
        "reason": "涉及售后、订单变更或其他需要人工确认的写操作",
        "display_message": (
            "这个请求需要人工客服确认后处理。请补充订单号、商品明细、诉求类型和问题描述，"
            "我会按人工接管入口整理信息。"
        ),
    },
    "out_of_scope": {
        "reason": "不属于 PC 外设商城客服的服务范围",
        "display_message": (
            "这个问题超出 PC 外设商城客服范围。我可以继续帮你做外设推荐、订单物流查询，"
            "或说明售后政策。"
        ),
    },
}


def boundary_for_classification(
    classification: str, reason: str | None = None
) -> BoundaryClassification:
    message = BOUNDARY_MESSAGES[classification]
    return BoundaryClassification(
        classification=classification,
        reason=reason or message["reason"],
        display_message=message["display_message"],
    )


def classify_boundary(message: str) -> BoundaryClassification:
    lowered = message.lower()
    compact = re.sub(r"\s+", "", lowered)

    if _requires_human_handoff(message, compact):
        return boundary_for_classification("human_handoff_required")

    if _is_explicitly_out_of_scope(message, lowered, compact):
        return boundary_for_classification(
            "out_of_scope",
            reason="问题明显超出 PC 外设商城客服范围",
        )

    return boundary_for_classification("in_scope_auto")


def _requires_human_handoff(message: str, compact: str) -> bool:
    if _requires_order_handoff(message, compact):
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


def _requires_order_handoff(message: str, compact: str) -> bool:
    if any(term in compact for term in ORDER_CHANGE_WRITE_TERMS):
        return True

    if not any(term in compact for term in PURCHASE_ACTION_TERMS):
        return False

    asks_for_info = any(term in compact for term in PURCHASE_INFO_TERMS)
    explicit_agent_action = re.search(
        r"(帮我|给我|替我|代我|客服|你).{0,8}(下单|支付|付款|提交订单|结算)",
        message,
    )
    user_direct_action = re.search(
        r"(我要|需要|现在|马上|直接).{0,8}(下单|支付|付款|提交订单|结算)",
        message,
    )
    direct_write_suffix = re.search(
        r"(下单|支付|付款|提交订单|结算).{0,6}(吧|一下|操作|办理|提交|完成)",
        message,
    )

    return bool(
        explicit_agent_action
        or direct_write_suffix
        or (user_direct_action and not asks_for_info)
    )


def _is_explicitly_out_of_scope(message: str, lowered: str, compact: str) -> bool:
    has_scope_signal = _has_strong_scope_signal(message, lowered, compact)
    return any(term in compact for term in OUT_OF_SCOPE_TERMS) and not has_scope_signal


def _has_strong_scope_signal(message: str, lowered: str, compact: str) -> bool:
    return (
        any(keyword in lowered or keyword in message for keyword in CATEGORY_KEYWORDS)
        or any(term in compact for term in ["订单", "物流", "快递", "发货"])
        or any(term in message for term in AFTER_SALES_TERMS)
        or any(term in compact for term in ["外设", "pc", "电脑"])
    )


def classify_intent(message: str) -> str:
    lowered = message.lower()
    compact = re.sub(r"\s+", "", lowered)
    if any(
        keyword in message
        for keyword in ["退货", "换货", "退款", "维修", "售后", "工单"]
    ):
        return "after_sales"
    if any(keyword in message for keyword in ["订单", "物流", "快递", "发货"]) or re.search(
        r"\b\d{8,}\b", lowered
    ):
        return "order_status"
    if _is_purchase_guidance(compact):
        return "purchase_guidance"
    if any(keyword in lowered for keyword in CATEGORY_KEYWORDS) or any(
        keyword in message for keyword in ["推荐", "预算", "买", "选", "对比"]
    ):
        return "product_recommendation"
    return "general"


def _is_purchase_guidance(compact: str) -> bool:
    return any(term in compact for term in PURCHASE_ACTION_TERMS) and any(
        term in compact for term in PURCHASE_INFO_TERMS
    )


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
    if any(keyword in message for keyword in ["麦克风", "带麦"]):
        filters["microphone"] = "是"

    query = message
    for word in [
        "推荐",
        "预算",
        "以内",
        "以下",
        "我想买",
        "买",
        "选",
        "怎么",
        "哪款",
        "哪个",
        "对比",
        "比较",
    ]:
        query = query.replace(word, " ")
    query = re.sub(r"\d+(?:\.\d+)?\s*(元|块)?", " ", query).strip()

    return ProductSearchRequest(
        query=query if len(query) > 1 else "",
        category=category,
        max_price=max_price,
        filters=filters,
        limit=6,
    )
