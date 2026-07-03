from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.dataset_mapper import normalize_part_record
from scripts.seed_demo import (
    _get_or_create_user,
    _seed_knowledge,
    _seed_order,
    _upsert_product,
)

TEST_PRODUCT_RECORDS = [
    (
        "mouse",
        {
            "name": "Logitech Codex G502 Hero",
            "price": 44.77,
            "tracking_method": "Optical",
            "connection_type": "Wired",
            "max_dpi": 25600,
            "hand_orientation": "Right",
            "color": "Black",
        },
    ),
    (
        "mouse",
        {
            "name": "Razer Codex Viper V3 Pro",
            "price": 139.99,
            "tracking_method": "Optical",
            "connection_type": "Wired, Wireless",
            "max_dpi": 35000,
            "hand_orientation": "Right",
            "color": "White",
        },
    ),
    (
        "headphones",
        {
            "name": "SteelSeries Codex Arctis Nova Wireless",
            "price": 49.88,
            "type": "Circumaural",
            "frequency_response": [20, 22],
            "microphone": True,
            "wireless": True,
            "enclosure_type": "Closed",
            "color": "Black",
        },
    ),
    (
        "headphones",
        {
            "name": "Beyerdynamic Codex DT 770 PRO",
            "price": 169.99,
            "type": "Circumaural",
            "frequency_response": [5, 35],
            "microphone": False,
            "wireless": False,
            "enclosure_type": "Closed",
            "color": "Gray",
        },
    ),
    (
        "keyboard",
        {
            "name": "RK Royal Kludge Codex RK61",
            "price": 49.99,
            "style": "Mini",
            "switches": "RK Red",
            "backlit": "RGB",
            "tenkeyless": True,
            "connection_type": "Wired, Wireless, Bluetooth Wireless",
            "color": "White",
        },
    ),
]


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncIterator[Callable[[], AsyncSession]]:
    test_engine = create_async_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    try:
        connection = await test_engine.connect()
        transaction = await connection.begin()
    except (OSError, DBAPIError, OperationalError) as exc:
        await test_engine.dispose()
        pytest.skip(f"PostgreSQL integration database is unavailable: {exc}")

    session_factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        async with session_factory() as session:
            user = await _get_or_create_user(session)
            first_sku = None
            for part_type, record in TEST_PRODUCT_RECORDS:
                product = normalize_part_record(part_type, record)
                assert product is not None
                sku = await _upsert_product(session, product)
                first_sku = first_sku or sku
            if first_sku:
                await _seed_order(session, user.id, first_sku)
            await _seed_knowledge(session)
            await session.commit()

        yield session_factory
    finally:
        await transaction.rollback()
        await connection.close()
        await test_engine.dispose()
