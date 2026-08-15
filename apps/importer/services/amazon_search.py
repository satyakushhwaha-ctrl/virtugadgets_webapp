"""Amazon search scraping service.

This module is deliberately limited to scraping and normalising search data.
Persistence is handled by the importer command/admin workflow.
"""

import logging
import os
import re
import tempfile
import time
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
    """Base class for Amazon search scraping failures."""


class AmazonNavigationError(AmazonSearchScrapingError):
    """Raised when Playwright did not receive a normal Amazon document."""


class AmazonSearchSelectorError(AmazonSearchScrapingError):
    """Raised when a normal Amazon document has no expected result selectors."""


class AmazonBlockedPageError(AmazonSearchScrapingError):
    """Raised when Amazon returns an anti-bot or interstitial page."""


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


def _response_diagnostics(response) -> dict[str, object]:
    if response is None:
        return {"status": None, "content_type": "", "headers": {}}
    try:
        status = response.status
    except Exception:
        status = None
    try:
        headers = response.headers
        headers = headers() if callable(headers) else headers
        headers = dict(headers or {})
    except Exception:
        headers = {}
    content_type = headers.get("content-type", "")
    return {
        "status": status if isinstance(status, int) else None,
        "content_type": content_type,
        "headers": headers,
    }


def _is_chrome_error_url(url: str) -> bool:
    return (url or "").lower().startswith("chrome-error://")


def _raise_navigation_error(
    page,
    keyword: str,
    requested_url: str,
    *,
    response=None,
    cause=None,
):
    diagnostics = _page_diagnostics(page)
    response_info = _response_diagnostics(response)
    screenshot_path, html_path = _save_page_diagnostics(page, keyword)
    message = (
        "Amazon search navigation failed. "
        f"Requested URL: {requested_url}; final URL: {diagnostics['url']}; "
        f"status: {response_info['status']}; content_type: {response_info['content_type'] or '<unknown>'}; "
        f"chrome_error_url: {_is_chrome_error_url(diagnostics['url'])}; "
        f"title: {diagnostics['title']}; diagnostics: screenshot={screenshot_path}, html={html_path}"
    )
    logger.warning(
        "Amazon search navigation diagnostic: %s response_headers=%s",
        message,
        response_info["headers"],
        exc_info=bool(cause),
    )
    raise AmazonNavigationError(message) from cause


def _log_navigation_diagnostics(page, keyword: str, requested_url: str, *, response=None, cause=None):
    """Persist diagnostics for a retryable navigation failure."""
    diagnostics = _page_diagnostics(page)
    response_info = _response_diagnostics(response)
    screenshot_path, html_path = _save_page_diagnostics(page, keyword)
    logger.warning(
        "Amazon search navigation attempt failed: requested_url=%s final_url=%s status=%s content_type=%s chrome_error_url=%s title=%s diagnostics: screenshot=%s, html=%s response_headers=%s",
        requested_url,
        diagnostics["url"],
        response_info["status"],
        response_info["content_type"] or "<unknown>",
        _is_chrome_error_url(diagnostics["url"]),
        diagnostics["title"],
        screenshot_path,
        html_path,
        response_info["headers"],
        exc_info=bool(cause),
    )


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
        error_type = AmazonBlockedPageError
    else:
        reason = "Amazon search product selectors were not found; page layout may have changed or returned no results."
        error_type = AmazonSearchSelectorError
    message = (
        f"{reason} URL: {diagnostics['url']}; title: {diagnostics['title']}; "
        f"diagnostics: screenshot={screenshot_path}, html={html_path}"
    )
    if cause:
        logger.warning("Amazon search diagnostic failure: %s", message, exc_info=True)
    else:
        logger.warning("Amazon search diagnostic failure: %s", message)
    raise error_type(message) from cause


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
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from .playwright import is_headless

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None
        requested_url = SEARCH_URL.format(keyword=quote_plus(keyword))
        navigation_attempts = 3
        try:
            browser = playwright.chromium.launch(
                headless=is_headless(),
                slow_mo=100,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                ],
            )
            for attempt in range(1, navigation_attempts + 1):
                context = None
                page = None
                response = None
                navigation_ready = False
                try:
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        locale="en-IN",
                        timezone_id="Asia/Kolkata",
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/138.0.0.0 Safari/537.36"
                        ),
                        extra_http_headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Accept-Language": "en-IN,en;q=0.9",
                            "Upgrade-Insecure-Requests": "1",
                        },
                    )
                    page = context.new_page()
                    page.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', "
                        "{get: () => undefined})"
                    )
                    response = page.goto(
                        requested_url,
                        wait_until="commit",
                        timeout=90000,
                    )
                    response_info = _response_diagnostics(response)
                    current_url = page.url
                    if _is_chrome_error_url(current_url):
                        raise AmazonNavigationError(
                            f"Amazon returned chrome-error:// for {requested_url}"
                        )
                    content_type = str(response_info["content_type"] or "").lower()
                    if content_type and not any(
                        value in content_type for value in ("text/html", "application/xhtml+xml")
                    ):
                        raise AmazonNavigationError(
                            f"Amazon returned non-HTML content ({content_type}) for {requested_url}"
                        )
                    page.wait_for_timeout(6000)
                    diagnostics = _page_diagnostics(page)
                    logger.info(
                        "Amazon search page before selector wait: requested_url=%s url=%s status=%s content_type=%s title=%s body=%s html=%s",
                        requested_url,
                        diagnostics["url"],
                        response_info["status"],
                        response_info["content_type"],
                        diagnostics["title"],
                        diagnostics["body_text"],
                        diagnostics["html_snippet"],
                    )
                    if _is_chrome_error_url(diagnostics["url"]):
                        raise AmazonNavigationError(
                            f"Amazon returned chrome-error:// for {requested_url}"
                        )
                    if _blocking_reason(diagnostics):
                        _raise_unexpected_page(page, keyword, diagnostics)
                    combined_selector = ", ".join(PRODUCT_SELECTORS)
                    try:
                        page.wait_for_selector(combined_selector, timeout=30000)
                    except PlaywrightTimeoutError as exc:
                        diagnostics = _page_diagnostics(page)
                        _raise_unexpected_page(page, keyword, diagnostics, cause=exc)
                    navigation_ready = True
                    break
                except PlaywrightTimeoutError as exc:
                    _log_navigation_diagnostics(
                        page, keyword, requested_url, response=response, cause=exc
                    )
                    if attempt == navigation_attempts:
                        _raise_navigation_error(
                            page, keyword, requested_url, response=response, cause=exc
                        )
                    logger.warning(
                        "Amazon search navigation timeout on attempt %s/%s: requested_url=%s",
                        attempt,
                        navigation_attempts,
                        requested_url,
                        exc_info=True,
                    )
                except PlaywrightError as exc:
                    if "Download is starting" not in str(exc):
                        raise
                    _log_navigation_diagnostics(
                        page, keyword, requested_url, response=response, cause=exc
                    )
                    logger.warning(
                        "Amazon search navigation returned a download on attempt %s/%s: requested_url=%s",
                        attempt,
                        navigation_attempts,
                        requested_url,
                        exc_info=True,
                    )
                    if attempt == navigation_attempts:
                        _raise_navigation_error(
                            page, keyword, requested_url, response=response, cause=exc
                        )
                except AmazonNavigationError as exc:
                    _log_navigation_diagnostics(
                        page, keyword, requested_url, response=response, cause=exc
                    )
                    if attempt == navigation_attempts:
                        _raise_navigation_error(
                            page, keyword, requested_url, response=response, cause=exc
                        )
                finally:
                    if page and not navigation_ready:
                        try:
                            page.close()
                        except Exception:
                            logger.debug("Could not close Amazon search page", exc_info=True)
                        page = None
                    if context and not navigation_ready:
                        try:
                            context.close()
                        except Exception:
                            logger.debug("Could not close Amazon search context", exc_info=True)
                        context = None
                if attempt < navigation_attempts:
                    time.sleep(attempt)

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
