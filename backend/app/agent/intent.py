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
AFTER_SALES_INFO_TERMS = [
    "政策",
    "规则",
    "流程",
    "说明",
    "多久",
    "几天",
    "期限",
    "时效",
    "多长时间",
    "条件",
    "材料",
    "怎么",
    "如何",
    "是否",
    "能否",
    "能不能",
    "可以",
    "什么",
    "哪些",
    "了解",
    "咨询",
    "问一下",
    "知道",
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
ORDER_CHANGE_ACTION_TERMS = ["取消", "撤销", "修改", "更改", "改", "换", "催", "补发"]
ORDER_CHANGE_OBJECT_TERMS = ["订单", "收货地址", "收货信息", "地址", "发货", "快递"]
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
    "条件",
    "要求",
    "是否",
    "能否",
    "能不能",
    "可以",
]
ACCOUNT_HANDOFF_TERMS = [
    "修改密码",
    "重置密码",
    "找回密码",
    "注销账号",
    "删除账号",
    "导出账户数据",
    "账号被盗",
    "账户被盗",
]
EXPLICIT_HANDOFF_TERMS = ["转人工", "人工客服", "找人工", "真人客服", "人工服务"]
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
THIRD_PARTY_TERMS = [
    "别人的",
    "他人的",
    "其他客户",
    "其他用户",
    "某个客户",
    "客户名单",
    "购买过的人",
    "哪些用户",
    "哪些客户",
    "同事的",
    "朋友的",
    "家人的",
]
PROTECTED_CUSTOMER_DATA_TERMS = [
    "订单",
    "购买记录",
    "买过",
    "收货地址",
    "地址",
    "手机号",
    "电话",
    "联系方式",
    "物流",
    "退款",
    "支付",
    "售后",
    "聊天记录",
    "客户信息",
    "个人信息",
]
SECRET_TERMS = ["密码", "验证码", "访问令牌", "access token", "api key", "支付凭证"]
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
    "unsupported": {
        "reason": "属于 PC 外设商城语境，但超出当前只读能力白名单",
        "display_message": (
            "这个需求属于商城相关场景，但当前客服能力还不能可靠完成。"
            "我可以继续协助商品查询、本人订单物流和商城政策咨询。"
        ),
    },
    "security_refusal": {
        "reason": "请求涉及其他客户数据或敏感凭证，不能查询或披露",
        "display_message": (
            "为了保护客户隐私和账户安全，我不能查询或披露其他客户的信息，也不能处理密码、"
            "验证码或支付凭证。你可以查询当前登录账号本人的订单和物流。"
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

    if requires_security_refusal(message):
        return boundary_for_classification("security_refusal")

    if _requires_human_handoff(message, compact):
        return boundary_for_classification("human_handoff_required")

    if _is_explicitly_out_of_scope(message, lowered, compact):
        return boundary_for_classification(
            "out_of_scope",
            reason="问题明显超出 PC 外设商城客服范围",
        )

    return boundary_for_classification("in_scope_auto")


def requires_security_refusal(message: str) -> bool:
    lowered = message.casefold()
    compact = re.sub(r"\s+", "", lowered)
    normalized_secret_terms = [re.sub(r"\s+", "", term.casefold()) for term in SECRET_TERMS]
    secret_expression = "|".join(re.escape(term) for term in normalized_secret_terms)
    discloses_secret = re.search(
        rf"(?:{secret_expression})(?:是|为|如下|[:：])",
        compact,
    )
    requests_secret = re.search(
        rf"(?:告诉|查看|读取|显示|导出|保存|记录|验证|处理).{{0,8}}"
        rf"(?:{secret_expression})",
        compact,
    )
    if discloses_secret or requests_secret:
        return True
    has_third_party = any(term in compact for term in THIRD_PARTY_TERMS)
    has_protected_data = any(term in compact for term in PROTECTED_CUSTOMER_DATA_TERMS)
    return has_third_party and has_protected_data


def requires_static_unsupported(message: str) -> bool:
    """Return whether a requested commerce action is outside the read-only capability list."""
    compact = re.sub(r"\s+", "", message.casefold())
    if _is_purchase_guidance(compact):
        return False
    if _requires_order_handoff(message, compact):
        return True
    unsupported_patterns = (
        r"(?:识别|扫描|扫一下|读取|提取).{0,8}(?:图片|照片|条形码|二维码|发票文件|pdf)",
        r"(?:图片|照片|条形码|二维码|发票文件|pdf).{0,8}(?:识别|扫描|读取|提取)",
        r"(?:到货|降价|价格).{0,6}(?:提醒|通知)",
        r"(?:设置|创建).{0,6}(?:到货|降价|价格).{0,6}(?:提醒|通知)",
        r"(?:锁定|预留|保留).{0,8}(?:商品|鼠标|键盘|耳机|显示器|库存)",
        r"(?:联系|询问).{0,6}(?:顺丰|快递|物流公司|承运商)",
        r"(?:视频|摄像头).{0,8}(?:诊断|检测|判断).{0,8}(?:故障|硬件|闪屏|损坏)",
    )
    unavailable_analytics = (
        "历史价格",
        "价格预测",
        "未来价格",
        "销量趋势",
        "销量增长率",
        "销量环比",
        "自动检测兼容性",
    )
    return any(re.search(pattern, compact) for pattern in unsupported_patterns) or any(
        marker in compact for marker in unavailable_analytics
    )


def _requires_human_handoff(message: str, compact: str) -> bool:
    if any(term in compact for term in EXPLICIT_HANDOFF_TERMS):
        return True
    if any(term in compact for term in ACCOUNT_HANDOFF_TERMS):
        return True
    if _requires_order_handoff(message, compact):
        return True
    account_workflow = (
        r"(?:修改|更改|改|绑定).{0,6}(?:登录邮箱|绑定邮箱|账户邮箱|账号邮箱)",
        r"(?:登录邮箱|绑定邮箱|账户邮箱|账号邮箱).{0,6}(?:修改|更改|改|绑定)",
        r"(?:导出).{0,12}(?:订单|账户数据|账号数据)",
        r"(?:订单|账户数据|账号数据).{0,12}(?:导出)",
        r"(?:删除|清除).{0,8}(?:偏好|记忆|个人数据)",
    )
    if any(re.search(pattern, compact) for pattern in account_workflow):
        return True

    has_after_sales = any(term in message for term in AFTER_SALES_TERMS)
    if not has_after_sales:
        return False

    after_sales_object = r"(?:退货|换货|退款|维修|保修|赔付|售后|工单)"
    action_verb = r"(?:申请|办理|提交|发起|创建|处理|安排)"

    # Assistance language is an explicit execution request even when phrased politely as a
    # question, for example “可以帮我申请退货吗”. Keep it narrower than a loose “帮我...退货”
    # match so “帮我看一下退货政策” remains an information request.
    assisted_action = re.search(
        rf"(?:帮我|给我|替我|为我|麻烦帮我)(?:{action_verb}(?:一下)?)?"
        rf"(?:把.{{0,8}})?{after_sales_object}",
        compact,
    )
    if assisted_action:
        return True

    asks_for_policy = any(term in compact for term in AFTER_SALES_INFO_TERMS)
    if asks_for_policy:
        return False

    first_person_action = re.search(
        rf"(?:我要|我想|我需要)(?:{action_verb}(?:一下)?)?"
        rf"(?:把.{{0,8}})?{after_sales_object}",
        compact,
    )
    explicit_operation = re.search(
        rf"{action_verb}.{{0,4}}{after_sales_object}",
        compact,
    )
    imperative_suffix = re.search(
        rf"{after_sales_object}(?:一下|吧|处理一下|办理一下)$",
        compact,
    )
    return bool(first_person_action or explicit_operation or imperative_suffix)


def _requires_order_handoff(message: str, compact: str) -> bool:
    asks_for_info = any(term in compact for term in PURCHASE_INFO_TERMS)
    has_known_write_phrase = any(term in compact for term in ORDER_CHANGE_WRITE_TERMS)
    has_action = any(term in compact for term in ORDER_CHANGE_ACTION_TERMS)
    has_object = any(term in compact for term in ORDER_CHANGE_OBJECT_TERMS)
    has_order_change = has_known_write_phrase or (has_action and has_object)
    explicit_order_change = re.search(
        r"(帮我|给我|替我|代我|麻烦|请|我要|我想|需要|现在|马上|直接)"
        r".{0,12}(取消|撤销|修改|更改|改|换|催|补发)"
        r".{0,12}(订单|收货地址|收货信息|地址|发货|快递)",
        message,
    )
    explicit_reverse_order_change = re.search(
        r"(订单|收货地址|收货信息|地址|发货|快递).{0,10}"
        r"(帮我|给我|替我|代我|麻烦|请).{0,6}"
        r"(取消|撤销|修改|更改|改|换|催|补发)",
        message,
    )
    if has_order_change and (
        explicit_order_change or explicit_reverse_order_change or not asks_for_info
    ):
        return True

    if not any(term in compact for term in PURCHASE_ACTION_TERMS):
        return False

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
