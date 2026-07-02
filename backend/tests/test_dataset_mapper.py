from decimal import Decimal

from app.services.dataset_mapper import attribute_flags, infer_brand, normalize_part_record


def test_normalizes_mouse_record_to_product_seed() -> None:
    product = normalize_part_record(
        "mouse",
        {
            "name": "Logitech G502 Hero",
            "price": 44.77,
            "tracking_method": "Optical",
            "connection_type": "Wired",
            "max_dpi": 25600,
            "hand_orientation": "Right",
            "color": "Black",
        },
    )

    assert product is not None
    assert product.category == "鼠标"
    assert product.brand == "Logitech"
    assert product.price_cny == Decimal("322.34")
    assert product.specs["color"] == "Black"
    assert product.attributes["max_dpi"] == "25600"


def test_skips_records_without_price() -> None:
    product = normalize_part_record("keyboard", {"name": "Wooting Two HE", "price": None})

    assert product is None


def test_infers_known_multi_word_brand() -> None:
    assert infer_brand("RK Royal Kludge RK61") == "RK Royal Kludge"


def test_attribute_flags_identify_specs_and_filters() -> None:
    assert attribute_flags("connection_type") == (True, True)
    assert attribute_flags("max_dpi") == (False, True)
