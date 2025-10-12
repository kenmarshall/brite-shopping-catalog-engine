from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from agent.config.settings import get_settings
from agent.utils.logging import get_logger

LOGGER = get_logger(__name__)


@asynccontextmanager
async def create_page() -> AsyncIterator[Page]:
    settings = get_settings().scraper
    playwright = await async_playwright().start()
    browser: Browser | None = None
    context: BrowserContext | None = None
    try:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context()
        page = await context.new_page()
        yield page
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


async def fetch_html(url: str) -> str:
    async with create_page() as page:
        LOGGER.info("Navigating to %s", url)
        await page.goto(url, wait_until="networkidle")
        content = await page.content()
        return content


__all__ = ["create_page", "fetch_html"]
