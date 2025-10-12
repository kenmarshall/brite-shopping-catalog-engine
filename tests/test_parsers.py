from pathlib import Path

from agent.scraping import parsers

FIXTURE = Path("tests/fixtures/demo_category.html").read_text()
SELECTORS = {
    "product": ".product",
    "name": ".name",
    "price": ".price",
    "image": "img::attr(src)",
    "size_hint": ".name",
    "brand_hint": ".brand",
    "category_hint": ".category",
}


def test_parse_products_extracts_two_items():
    raw = parsers.parse_products(FIXTURE, SELECTORS, "https://example.com")
    assert len(raw) == 2
    assert raw[0].price == 345.0
    assert raw[0].brand_hint == "Grace"


def test_raw_to_product_builds_checksum():
    raw = parsers.parse_products(FIXTURE, SELECTORS, "https://example.com")[0]
    product = parsers.raw_to_product(raw, store_id="demo", store_name="Demo Grocer")
    assert product.checksum
    assert product.normalized_name == "grace baked beans 400 g"
