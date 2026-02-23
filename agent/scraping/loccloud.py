"""LocCloud/ExtJS e-store scraper (used by Hi-Lo Food Stores).

This module handles stores built on the LocCloud POS platform, which uses
a Sencha ExtJS SPA frontend backed by a `trs.exe` XML API. Standard CSS
selector scraping doesn't work — we need to:

1. Launch Playwright and load the SPA
2. Log in via the ExtJS login form to obtain a session token (CN)
3. Fetch the full product catalog via the trs.exe XML API
4. Parse the XML records into RawProduct objects

XML field mapping (from trs.exe responses):
  F01  = barcode / item code
  F02  = full description (name + size)
  F17  = internal price code
  F22  = size string (e.g., "432G", "500ML")
  F29  = product name
  F155 = brand
  F255 = category (often empty)
  F1000 = department code
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from typing import Any

from agent.scraping.parsers import RawProduct
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)

BASE_URL = "https://hilofoodstoresja.loccloud.net"
TRS_URL = f"{BASE_URL}/scripts/trs.exe"
SPA_URL = f"{BASE_URL}/xstore/index.html#desktop=pos.desktopproduct"


@dataclass
class LocCloudSession:
    cn_token: str
    page: Any  # playwright Page object


async def login_and_get_session(
    email: str | None = None,
    password: str | None = None,
    *,
    headless: bool = True,
) -> tuple[Any, Any, LocCloudSession]:
    """Launch Playwright, log into Hi-Lo, and return (browser, context, session).

    The caller is responsible for closing the browser when done.
    """
    from playwright.async_api import async_playwright

    if not email or not password:
        # Load from .env using dotenv (handles special characters properly)
        from dotenv import dotenv_values

        env = dotenv_values(".env")
        email = email or env.get("HILO_EMAIL") or os.environ.get("HILO_EMAIL", "")
        password = password or env.get("HILO_PASSWORD") or os.environ.get("HILO_PASSWORD", "")
    if not email or not password:
        raise ValueError(
            "Hi-Lo credentials required. Set HILO_EMAIL and HILO_PASSWORD in .env"
        )

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=headless)

    # Try loading saved session first
    storage_path = "./data/sessions/hilo.json"
    context_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
    if os.path.exists(storage_path):
        context_kwargs["storage_state"] = storage_path

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()

    cn_token: str | None = None

    async def capture_cn(response: Any) -> None:
        nonlocal cn_token
        url = response.url
        if "trs.exe" in url:
            m = re.search(r"CN=([A-Z]{10,})", url)
            if m:
                cn_token = m.group(1)

    page.on("response", capture_cn)

    LOGGER.info("Loading Hi-Lo SPA...")
    await page.goto(SPA_URL, wait_until="networkidle", timeout=45000)
    await page.wait_for_timeout(8000)

    if cn_token:
        LOGGER.info("Already authenticated (CN=%s...)", cn_token[:6])
    else:
        LOGGER.info("Logging in to Hi-Lo...")

        # The login form is a modal that appears on load.
        # Email field: input with placeholder "User code or Email"
        # Password field: input[type="Password"]
        email_input = await page.query_selector('input[placeholder*="Email"]')
        if not email_input:
            email_input = await page.query_selector('input[role="textbox"][type="text"]')

        if email_input:
            await email_input.click()
            await email_input.fill(email)
        else:
            LOGGER.warning("Could not find email input")

        password_input = await page.query_selector('input[type="Password"], input[type="password"]')
        if password_input:
            await password_input.fill(password)
            await page.wait_for_timeout(500)

        # Click the "Sign in" button (Enter key doesn't work reliably)
        buttons = await page.query_selector_all('[class*="btn"]')
        for btn in buttons:
            text = (await btn.inner_text()).strip()
            if "sign in" in text.lower():
                await btn.click()
                await page.wait_for_timeout(10000)
                break
        else:
            # Fallback: press Enter
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(10000)

        if not cn_token:
            await browser.close()
            raise RuntimeError("Failed to obtain Hi-Lo session token after login")

        LOGGER.info("Login successful (CN=%s...)", cn_token[:6])

        # Save session state
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)
        await context.storage_state(path=storage_path)

    return browser, context, LocCloudSession(cn_token=cn_token, page=page)


async def fetch_catalog_xml(session: LocCloudSession, max_records: int = 2000) -> str:
    """Fetch the full product catalog XML from the trs.exe API."""
    url = (
        f"{TRS_URL}?cgi=eStore_pos_itm_catalog.xml"
        f"&ExtGridUsp=eStore_pos_itm_searchGeneral"
        f"&ExtGridSort=F29"
        f"&ExtMaxRecords={max_records}"
        f"&loyaltyitems=0&F1232E=0&Top={max_records}"
        f"&ExtGridStep=1&ExtGridSession=10&ExtGridAlias=CommonGrid"
        f"&BW=CH&DN=eStore&CN={session.cn_token}&dbase_timeout=20"
    )
    LOGGER.info("Fetching catalog (max %d records)...", max_records)
    result = await session.page.evaluate(
        """async (url) => {
            const r = await fetch(url);
            return await r.text();
        }""",
        url,
    )
    return result


async def fetch_categories_xml(session: LocCloudSession) -> str:
    """Fetch the category list XML."""
    url = (
        f"{TRS_URL}?cgi=eStore_pos_cat_list.xml"
        f"&ExtGridAlias=ComboQuery"
        f"&BW=CH&DN=eStore&CN={session.cn_token}&dbase_timeout=20"
    )
    result = await session.page.evaluate(
        """async (url) => {
            const r = await fetch(url);
            return await r.text();
        }""",
        url,
    )
    return result


def parse_catalog_xml(xml_text: str) -> list[RawProduct]:
    """Parse the trs.exe catalog XML into RawProduct objects."""
    products: list[RawProduct] = []

    # The XML has <record> elements with <F01>, <F29>, etc. fields
    records = re.findall(r"<record>(.*?)</record>", xml_text, re.DOTALL)
    LOGGER.info("Parsing %d XML records", len(records))

    for record_xml in records:
        fields: dict[str, str] = {}
        for match in re.finditer(r"<(F\d+)>(.*?)</\1>", record_xml, re.DOTALL):
            fields[match.group(1)] = unescape(match.group(2)).strip()

        name = fields.get("F29", "").strip()
        if not name:
            continue

        # Parse price — F30 is the retail price, F1007 is also price (usually same)
        price: float | None = None
        for price_field in ["F30", "F1007"]:
            raw_price = fields.get(price_field, "")
            if raw_price:
                try:
                    price = float(raw_price.replace(",", ""))
                    break
                except ValueError:
                    continue

        # Build image URL from F2929 (image filename) or barcode
        image_filename = fields.get("F2929", "").strip()
        barcode = fields.get("F01", "")
        if image_filename:
            image_url = f"{BASE_URL}/Bitmaps/items/{image_filename}"
        elif barcode:
            image_url = f"{BASE_URL}/Bitmaps/items/{barcode}.jpg"
        else:
            image_url = None

        brand = fields.get("F155", "").strip() or None
        size_hint = fields.get("F22", "").strip() or None
        category = fields.get("F255", "").strip() or None

        # Use full description (F02) as fallback for name if richer
        full_desc = fields.get("F02", "").strip()

        products.append(
            RawProduct(
                name=full_desc if full_desc and len(full_desc) > len(name) else name,
                price=price,
                currency="JMD",
                image_url=image_url,
                size_hint=size_hint or name,
                brand_hint=brand,
                category_hint=category,
                url=f"{SPA_URL}&item={barcode}" if barcode else SPA_URL,
                barcode=barcode if barcode else None,
            )
        )

    return products


__all__ = [
    "login_and_get_session",
    "fetch_catalog_xml",
    "fetch_categories_xml",
    "parse_catalog_xml",
    "LocCloudSession",
]
