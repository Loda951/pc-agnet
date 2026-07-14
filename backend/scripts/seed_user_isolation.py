from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

USER_COUNT = 5
ORDERS_PER_USER = 5
CONVERSATIONS_PER_USER = 2
DEMO_PASSWORD = "isolation-demo-password"
ORDER_ID_BASE = 991_000_000_000
CONVERSATION_ID_BASE = 992_000_000_000
MESSAGE_ID_BASE = 993_000_000_000
ORDER_ITEM_ID_BASE = 994_000_000_000


class SeedOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserSeedSpec:
    ordinal: int
    username: str
    login_identifier: str
    phone: str


@dataclass(frozen=True)
class OrderSeedSpec:
    id: int
    item_id: int
    user_ordinal: int
    sequence: int
    item_price: Decimal
    total_amount: Decimal
    pay_amount: Decimal
    freight_amount: Decimal
    pay_type: int
    status: int
    created_at: datetime
    pay_at: datetime | None
    delivery_at: datetime | None


@dataclass(frozen=True)
class MessageSeedSpec:
    id: int
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationSeedSpec:
    id: int
    user_ordinal: int
    username: str
    sequence: int
    title: str
    created_at: datetime
    messages: tuple[MessageSeedSpec, MessageSeedSpec]


@dataclass(frozen=True)
class UserSummary:
    user_id: int
    username: str
    login_identifier: str


@dataclass(frozen=True)
class OrderSummary:
    order_id: int
    user_id: int
    username: str
    pay_amount: Decimal
    status: int
    created_at: datetime


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: int
    user_id: int
    username: str
    title: str


@dataclass(frozen=True)
class SeedSummary:
    users: tuple[UserSummary, ...]
    orders: tuple[OrderSummary, ...]
    conversations: tuple[ConversationSummary, ...]


def build_user_specs() -> tuple[UserSeedSpec, ...]:
    return tuple(
        UserSeedSpec(
            ordinal=ordinal,
            username=f"test_user_{ordinal:03d}",
            login_identifier=f"test_user_{ordinal:03d}@example.com",
            phone=f"13900000{ordinal:03d}",
        )
        for ordinal in range(1, USER_COUNT + 1)
    )


def build_order_specs(user_ordinal: int, anchor: datetime) -> tuple[OrderSeedSpec, ...]:
    specs = []
    for sequence in range(1, ORDERS_PER_USER + 1):
        created_at = anchor - timedelta(
            days=(user_ordinal - 1) * ORDERS_PER_USER + sequence
        )
        status = sequence
        item_price = Decimal(100 + user_ordinal * 10 + sequence * 7).quantize(
            Decimal("0.01")
        )
        freight = Decimal("0.00") if sequence in {3, 4} else Decimal("10.00")
        total = item_price + freight
        paid = status in {2, 3, 4}
        shipped = status in {3, 4}
        specs.append(
            OrderSeedSpec(
                id=ORDER_ID_BASE + user_ordinal * 100 + sequence,
                item_id=ORDER_ITEM_ID_BASE + user_ordinal * 100 + sequence,
                user_ordinal=user_ordinal,
                sequence=sequence,
                item_price=item_price,
                total_amount=total,
                pay_amount=total if paid else Decimal("0.00"),
                freight_amount=freight,
                pay_type=2 if paid else 0,
                status=status,
                created_at=created_at,
                pay_at=created_at + timedelta(minutes=8) if paid else None,
                delivery_at=created_at + timedelta(hours=6) if shipped else None,
            )
        )
    return tuple(specs)


def build_conversation_specs(
    user_ordinal: int, anchor: datetime
) -> tuple[ConversationSeedSpec, ...]:
    username = f"test_user_{user_ordinal:03d}"
    specs = []
    for sequence in range(1, CONVERSATIONS_PER_USER + 1):
        conversation_id = CONVERSATION_ID_BASE + user_ordinal * 100 + sequence
        created_at = anchor - timedelta(hours=user_ordinal * 3 + sequence)
        messages = (
            MessageSeedSpec(
                id=MESSAGE_ID_BASE + user_ordinal * 1000 + sequence * 10 + 1,
                role="user",
                content=f"我是 {username}，请查询我的隔离测试订单。",
                created_at=created_at,
            ),
            MessageSeedSpec(
                id=MESSAGE_ID_BASE + user_ordinal * 1000 + sequence * 10 + 2,
                role="assistant",
                content=f"已进入 {username} 的隔离测试会话 {sequence:02d}。",
                created_at=created_at + timedelta(seconds=5),
            ),
        )
        specs.append(
            ConversationSeedSpec(
                id=conversation_id,
                user_ordinal=user_ordinal,
                username=username,
                sequence=sequence,
                title=f"{username} 隔离测试会话 {sequence:02d}",
                created_at=created_at,
                messages=messages,
            )
        )
    return tuple(specs)


def assert_expected_owner(
    record_type: str,
    record_id: int,
    actual_owner_id: int,
    expected_owner_id: int,
) -> None:
    if actual_owner_id != expected_owner_id:
        raise SeedOwnershipError(
            f"{record_type} {record_id} belongs to owner {actual_owner_id}; "
            f"expected {expected_owner_id}"
        )


def format_summary(summary: SeedSummary) -> str:
    lines = ["用户隔离 Mock 数据写入完成", "", "[users]"]
    lines.extend(
        f"user_id={item.user_id} username={item.username} "
        f"login={item.login_identifier} password={DEMO_PASSWORD}"
        for item in summary.users
    )
    lines.extend(("", "[orders]"))
    lines.extend(
        f"order_id={item.order_id} user_id={item.user_id} owner={item.username} "
        f"amount={item.pay_amount:.2f} status={item.status} "
        f"created_at={item.created_at.isoformat()}"
        for item in summary.orders
    )
    lines.extend(("", "[conversations]"))
    lines.extend(
        f"conversation_id={item.conversation_id} user_id={item.user_id} "
        f"owner={item.username} title={item.title}"
        for item in summary.conversations
    )
    return "\n".join(lines)
