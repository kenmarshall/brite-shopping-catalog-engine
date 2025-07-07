from typing import List, Dict

import requests
from bs4 import BeautifulSoup
from langchain.tools import tool

from utils.logger import get_logger

logger = get_logger(__name__)


@tool
def scrape_and_parse(url: str) -> List[Dict]:
    """Scrape product data from the given URL."""
    logger.info("Scraping %s", url)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Request failed: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    products = []
    for item in soup.select(".product-item"):
        title_el = item.select_one(".product-title")
        price_el = item.select_one(".price")
        if not title_el or not price_el:
            continue
        products.append({
            "name": title_el.get_text(strip=True),
            "price": price_el.get_text(strip=True),
            "url": url,
        })
    logger.info("Found %d products", len(products))
    return products
