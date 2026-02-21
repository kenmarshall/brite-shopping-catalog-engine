from agent.db.models import SizeInfo
from agent.scraping.normalizer import (
    build_checksum,
    normalize_brand,
    normalize_category,
    normalize_display_name,
    normalize_name,
    parse_price,
    parse_size,
    strip_size_from_name,
)


def test_normalize_name():
    assert normalize_name("Grace Baked Beans - 400g") == "grace baked beans 400g"


def test_parse_price():
    assert parse_price("$345.00") == 345.0
    assert parse_price("J$1,200") == 1200.0


def test_parse_size():
    size = parse_size("Grace Baked Beans 400 g")
    assert size.value == 400.0
    assert size.unit == "g"
    assert size.pack_count is None


def test_parse_size_multipack():
    size = parse_size("Pepsi 6x330ml")
    assert size.value == 330.0
    assert size.unit == "ml"
    assert size.pack_count == 6


def test_strip_size_from_name():
    assert strip_size_from_name("Grace Coconut Milk 400ml") == "Grace Coconut Milk"
    assert strip_size_from_name("Pepsi 6x330ml") == "Pepsi"


def test_normalize_display_name():
    assert normalize_display_name("GRACE BAKED BEANS") == "Grace Baked Beans"


def test_build_checksum_changes_with_size():
    size_a = SizeInfo(value=400.0, unit="g")
    size_b = SizeInfo(value=500.0, unit="g")
    checksum_a = build_checksum("demo", "grace baked beans", "Grace", size_a)
    checksum_b = build_checksum("demo", "grace baked beans", "Grace", size_b)
    assert checksum_a != checksum_b


def test_build_checksum_changes_with_pack_count():
    size_a = SizeInfo(value=330.0, unit="ml", pack_count=1)
    size_b = SizeInfo(value=330.0, unit="ml", pack_count=6)
    checksum_a = build_checksum("demo", "pepsi", "Pepsi", size_a)
    checksum_b = build_checksum("demo", "pepsi", "Pepsi", size_b)
    assert checksum_a != checksum_b


def test_parse_size_gallon():
    size = parse_size("Wata 1Gal")
    assert size.value == 1.0
    assert size.unit == "gal"
    assert size.pack_count is None


def test_parse_size_gallon_variants():
    for text in ["5 gallon", "2gallons", "1gal"]:
        size = parse_size(text)
        assert size.unit == "gal", f"Failed for '{text}': got {size.unit}"
        assert size.value is not None, f"Failed for '{text}': no value"


def test_parse_size_pint_quart():
    size = parse_size("Milk 1pt")
    assert size.value == 1.0
    assert size.unit == "pt"

    size = parse_size("Juice 1 quart")
    assert size.value == 1.0
    assert size.unit == "qt"


def test_parse_size_cl_mg():
    size = parse_size("Perfume 50cl")
    assert size.value == 50.0
    assert size.unit == "cl"

    size = parse_size("Vitamin C 500mg")
    assert size.value == 500.0
    assert size.unit == "mg"


def test_normalize_brand_category():
    assert normalize_brand("grace") == "Grace"
    assert normalize_category(" canned goods ") == "Canned Goods"
