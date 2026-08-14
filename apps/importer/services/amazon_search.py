"""Amazon search scraping service.

This module is deliberately limited to scraping and normalising search data.
Persistence is handled by the importer command/admin workflow.
"""

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from django.core.exceptions import ValidationError


logger = logging.getLogger(__name__)


AMAZON_BASE_URL = "https://www.amazon.in"
SEARCH_URL = f"{AMAZON_BASE_URL}/s?k={{keyword}}"
ASIN_PATTERN = re.compile(r"(?i)(?:/dp/|/gp/product/|/gp/aw/d/)([A-Z0-9]{10})")
PRODUCT_SELECTORS = (
    "div[data-component-type='s-search-result']",
    "div.s-result-item[data-asin]",
    "[data-asin]",
)
BLOCK_MARKERS = (
    "captcha",
    "robot check",
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "unusual traffic",
    "access denied",
    "sign in to continue",
    "consent required",
)


class AmazonSearchScrapingError(RuntimeError):
    """Raised when Amazon returns a block/interstitial/unexpected page."""


def _page_diagnostics(page) -> dict[str, str]:
    """Collect bounded diagnostics without making another network request."""
    try:
        url = page.url
    except Exception:
        url = "<unavailable>"
    try:
        title = page.title()
    except Exception:
        title = "<unavailable>"
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = "<body text unavailable>"
    try:
        html = page.content()
    except Exception:
        html = "<page HTML unavailable>"
    return {
        "url": url,
        "title": title,
        "body_text": " ".join(body_text.split())[:4000],
        "html_snippet": html[:5000],
        "html": html,
    }


def _blocking_reason(diagnostics: dict[str, str]) -> str | None:
    haystack = " ".join(
        diagnostics.get(field, "")
        for field in ("url", "title", "body_text", "html_snippet")
    ).lower()
    for marker in BLOCK_MARKERS:
        if marker in haystack:
            return marker
    return None


def _save_page_diagnostics(page, keyword: str) -> tuple[str, str]:
    diagnostics = _page_diagnostics(page)
    safe_keyword = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:60] or "search"
    directory = Path(
        os.getenv("PLAYWRIGHT_DIAGNOSTICS_DIR", tempfile.gettempdir())
    ) / f"amazon-search-{safe_keyword}-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=True)
    screenshot_path = directory / "page.png"
    html_path = directory / "page.html"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as exc:
        logger.warning("Could not save Amazon diagnostic screenshot: %s", exc)
    html_path.write_text(diagnostics["html"], encoding="utf-8")
    return str(screenshot_path), str(html_path)


def _raise_unexpected_page(page, keyword: str, diagnostics: dict[str, str], cause=None):
    block_reason = _blocking_reason(diagnostics)
    screenshot_path, html_path = _save_page_diagnostics(page, keyword)
    if block_reason:
        reason = f"Amazon returned a blocking/interstitial page ({block_reason})."
    else:
        reason = "Amazon search product selectors were not found; page layout may have changed or returned no results."
    message = (
        f"{reason} URL: {diagnostics['url']}; title: {diagnostics['title']}; "
        f"diagnostics: screenshot={screenshot_path}, html={html_path}"
    )
    if cause:
        logger.warning("Amazon search diagnostic failure: %s", message, exc_info=True)
    else:
        logger.warning("Amazon search diagnostic failure: %s", message)
    raise AmazonSearchScrapingError(message) from cause


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
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from .playwright import is_headless

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None
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
            diagnostics = _page_diagnostics(page)
            logger.info(
                "Amazon search page before selector wait: url=%s title=%s body=%s html=%s",
                diagnostics["url"],
                diagnostics["title"],
                diagnostics["body_text"],
                diagnostics["html_snippet"],
            )
            if _blocking_reason(diagnostics):
                _raise_unexpected_page(page, keyword, diagnostics)
            combined_selector = ", ".join(PRODUCT_SELECTORS)
            try:
                page.wait_for_selector(combined_selector, timeout=30000)
            except PlaywrightTimeoutError as exc:
                diagnostics = _page_diagnostics(page)
                _raise_unexpected_page(page, keyword, diagnostics, cause=exc)

            for _ in range(5):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(800)

            products = None
            for selector in PRODUCT_SELECTORS:
                candidate_products = page.locator(selector)
                if candidate_products.count():
                    products = candidate_products
                    break
            if products is None:
                diagnostics = _page_diagnostics(page)
                _raise_unexpected_page(page, keyword, diagnostics)
            rows = []
            for index in range(products.count()):
                item = products.nth(index)
                item_asin = item.get_attribute("data-asin")
                link = item.locator("h2 a, a[href*='/dp/'], a[href*='/gp/product/']")
                href = link.first.get_attribute("href") if link.count() else None
                title = item.locator("h2 span, h2, a[href*='/dp/'], a[href*='/gp/product/']")
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
            if page:
                try:
                    page.close()
                except Exception:
                    logger.debug("Could not close Amazon search page", exc_info=True)
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
