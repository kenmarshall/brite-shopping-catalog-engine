from agent.db.models import SizeInfo
from agent.scraping.normalizer import (
    build_checksum,
    normalize_brand,
    normalize_category,
    normalize_name,
    parse_price,
    parse_size,
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


def test_build_checksum_changes_with_size():
    size_a = SizeInfo(value=400.0, unit="g")
    size_b = SizeInfo(value=500.0, unit="g")
    checksum_a = build_checksum("demo", "grace baked beans", "Grace", size_a)
    checksum_b = build_checksum("demo", "grace baked beans", "Grace", size_b)
    assert checksum_a != checksum_b


def test_normalize_brand_category():
    assert normalize_brand("grace") == "Grace"
    assert normalize_category(" canned goods ") == "Canned Goods"
