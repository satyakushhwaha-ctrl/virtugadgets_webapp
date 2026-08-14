"""Amazon search scraping service.

This module is deliberately limited to scraping and normalising search data.
Persistence is handled by the importer command/admin workflow.
"""

import re
from urllib.parse import quote_plus, urljoin, urlparse

from django.core.exceptions import ValidationError


AMAZON_BASE_URL = "https://www.amazon.in"
SEARCH_URL = f"{AMAZON_BASE_URL}/s?k={{keyword}}"
ASIN_PATTERN = re.compile(r"(?i)(?:/dp/|/gp/product/|/gp/aw/d/)([A-Z0-9]{10})")


def _normalise_amazon_url(href: str | None, asin: str | None) -> str | None:
    """Return the canonical Amazon product URL when an ASIN is available."""
    if asin:
        return f"{AMAZON_BASE_URL}/dp/{asin.upper()}"
    if not href:
        return None

    absolute_url = urljoin(AMAZON_BASE_URL, href)
    parsed = urlparse(absolute_url)
    if parsed.netloc and not parsed.netloc.lower().endswith("amazon.in"):
        return None
    return absolute_url.split("?")[0].split("#")[0]


def _extract_asin(asin: str | None, href: str | None) -> str | None:
    if asin:
        candidate = asin.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{10}", candidate):
            return candidate
    if href:
        match = ASIN_PATTERN.search(href)
        if match:
            return match.group(1).upper()
    return None


def _scrape_search_results(keyword: str) -> list[dict]:
    """Scrape Amazon using the project's existing Playwright behaviour."""
    # Keep Playwright lazy so Django checks and unit tests do not require a
    # browser installation unless a real search is actually requested.
    from playwright.sync_api import sync_playwright
    from .playwright import is_headless

    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(
                headless=is_headless(),
                slow_mo=100,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-infobars",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
            page.goto(
                SEARCH_URL.format(keyword=quote_plus(keyword)),
                wait_until="commit",
                timeout=90000,
            )
            page.wait_for_timeout(6000)
            page.wait_for_selector(
                "div[data-component-type='s-search-result']",
                timeout=30000,
            )

            for _ in range(5):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(800)

            products = page.locator(
                "div[data-component-type='s-search-result']"
            )
            rows = []
            for index in range(products.count()):
                item = products.nth(index)
                item_asin = item.get_attribute("data-asin")
                link = item.locator("h2 a")
                href = link.first.get_attribute("href") if link.count() else None
                title = item.locator("h2 span")
                rows.append(
                    {
                        "asin": item_asin,
                        "title": (
                            title.first.inner_text().strip()
                            if title.count()
                            else ""
                        ),
                        "product_url": href,
                        "position": index + 1,
                        "sponsored": "sponsored" in item.inner_text().lower(),
                    }
                )
            return rows
        finally:
            if context:
                context.close()
            if browser:
                browser.close()


def search_amazon(keyword: str) -> list[dict]:
    """Search Amazon and return unique, normalised result dictionaries."""
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValidationError("Amazon search keyword cannot be empty.")

    normalised_keyword = keyword.strip()
    results = _scrape_search_results(normalised_keyword)
    normalised = []
    seen_asins = set()

    for result in results:
        asin = _extract_asin(result.get("asin"), result.get("product_url"))
        if not asin or asin in seen_asins:
            continue
        product_url = _normalise_amazon_url(result.get("product_url"), asin)
        if not product_url:
            continue
        seen_asins.add(asin)
        normalised.append(
            {
                "asin": asin,
                "title": (result.get("title") or "").strip(),
                "product_url": product_url,
                "position": result.get("position") or len(normalised) + 1,
                "sponsored": bool(result.get("sponsored", False)),
            }
        )

    return normalised
