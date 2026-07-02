from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha1
from typing import Any

CATEGORY_NAME_MAP = {
    "mouse": "鼠标",
    "keyboard": "键盘",
    "headphones": "耳机",
    "monitor": "显示器",
    "webcam": "摄像头",
    "speakers": "音箱",
}

KNOWN_BRANDS = [
    "Logitech",
    "Razer",
    "SteelSeries",
    "Corsair",
    "Asus",
    "ROG",
    "Redragon",
    "Glorious",
    "HyperX",
    "HP",
    "Microsoft",
    "Wooting",
    "RK Royal Kludge",
    "Royal Kludge",
]

FILTER_ATTRIBUTES = {
    "color",
    "connection_type",
    "tracking_method",
    "max_dpi",
    "hand_orientation",
    "style",
    "switches",
    "backlit",
    "tenkeyless",
    "wireless",
    "microphone",
    "panel_type",
    "refresh_rate",
    "resolution",
}

SPEC_ATTRIBUTES = {
    "color",
    "connection_type",
    "switches",
    "backlit",
    "tenkeyless",
    "wireless",
    "microphone",
}


@dataclass(frozen=True)
class ImportedProduct:
    category: str
    brand: str
    spu_title: str
    sku_title: str
    price_cny: Decimal
    stock: int
    specs: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)


def infer_brand(name: str) -> str:
    lowered = name.lower()
    for brand in KNOWN_BRANDS:
        if lowered.startswith(brand.lower()):
            return brand
    return name.split(" ", 1)[0]


def normalize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return " / ".join(str(item) for item in value)
    return str(value)


def normalize_part_record(
    part_type: str,
    record: dict[str, Any],
    exchange_rate: Decimal = Decimal("7.20"),
) -> ImportedProduct | None:
    name = normalize_value(record.get("name"))
    price = record.get("price")
    if not name or price is None:
        return None

    brand = infer_brand(name)
    category = CATEGORY_NAME_MAP.get(part_type, part_type)
    price_cny = (Decimal(str(price)) * exchange_rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
    attributes = {
        key: value
        for key, raw in record.items()
        if key not in {"name", "price"} and (value := normalize_value(raw)) is not None
    }
    specs = {key: value for key, value in attributes.items() if key in SPEC_ATTRIBUTES}

    sku_suffix = []
    for key in ("color", "connection_type", "switches"):
        value = attributes.get(key)
        if value and value.lower() not in name.lower():
            sku_suffix.append(value)
    sku_title = " ".join([name, *sku_suffix[:2]])

    digest = sha1(name.encode("utf-8")).hexdigest()
    stock = 10 + int(digest[:2], 16) % 90

    return ImportedProduct(
        category=category,
        brand=brand,
        spu_title=name,
        sku_title=sku_title,
        price_cny=price_cny,
        stock=stock,
        specs=specs,
        attributes=attributes,
    )


def attribute_flags(attr_name: str) -> tuple[bool, bool]:
    return attr_name in SPEC_ATTRIBUTES, attr_name in FILTER_ATTRIBUTES
