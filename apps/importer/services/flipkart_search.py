"""Flipkart candidate discovery service."""

import logging
import re
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from django.core.exceptions import ValidationError


FLIPKART_BASE_URL = "https://www.flipkart.com"
SEARCH_URL = f"{FLIPKART_BASE_URL}/search?q={{query}}"
PID_PATTERN = re.compile(r"(?i)(?:[?&]pid=|/p/[^/?#]*-)([A-Z0-9]+)")
CAPACITY_PATTERN = re.compile(
    r"^(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)"
    r"(?:\s*(?:RAM|ROM|STORAGE))?$",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def _normalise_capacity(value: str | None, kind: str) -> str | None:
    """Normalize valid RAM/storage values and reject weight-like values."""
    value = " ".join((value or "").split())
    match = CAPACITY_PATTERN.fullmatch(value)
    if not match:
        return None
    amount = match.group("amount")
    unit = match.group("unit").upper()
    if kind == "ram" and ("." in amount or float(amount) < 1):
        return None
    if kind == "storage" and float(amount) < 1:
        return None
    if amount.endswith(".0"):
        amount = amount[:-2]
    return f"{amount} {unit}"


def _normalise_title(title: str) -> str:
    title = " ".join((title or "").split())
    title = re.sub(r"\([^)]*\)", "", title)
    title = re.split(r"\s*[,:;|–—]\s*", title, maxsplit=1)[0]
    title = re.split(r"\s+with\s+", title, maxsplit=1, flags=re.IGNORECASE)[0]
    return re.sub(
        r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)"
        r"(?:\s*(?:RAM|ROM|STORAGE))?",
        lambda match: f"{match.group('amount')} {match.group('unit').upper()}",
        title,
        flags=re.IGNORECASE,
    ).strip()


def _append_unique(parts: list[str], value: str | None) -> None:
    if not value:
        return
    existing = " ".join(parts).casefold()
    if not re.search(
        rf"(?<!\w){re.escape(value.casefold())}(?!\w)",
        existing,
    ):
        parts.append(value)


def _query_parts(
    amazon_product,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    title = _normalise_title(amazon_product.product_title)
    brand = " ".join((amazon_product.brand or "").split())
    if brand and title.casefold().startswith(brand.casefold()):
        identity = title[len(brand):].strip()
    else:
        identity = title

    storage = _normalise_capacity(amazon_product.storage, "storage")
    ram = _normalise_capacity(amazon_product.ram, "ram")
    color = " ".join((amazon_product.color or "").split()) or None
    if storage:
        identity = re.sub(
            r"\b\d+(?:\.\d+)?\s*(?:GB|TB)\s*"
            r"(?:RAM|ROM|STORAGE)?\b",
            " ",
            identity,
            flags=re.IGNORECASE,
        )
    if color:
        identity = re.sub(
            rf"\b{re.escape(color)}\b",
            " ",
            identity,
            flags=re.IGNORECASE,
        )
    identity = " ".join(identity.split())
    return brand, identity, storage, ram, color


def build_flipkart_search_queries(amazon_product) -> list[str]:
    """Build at most three progressively broader product identity queries."""
    brand, identity, storage, ram, color = _query_parts(amazon_product)
    def make_query(include_color: bool, include_storage: bool) -> str:
        parts = []
        _append_unique(parts, brand)
        _append_unique(parts, identity)
        if include_storage:
            _append_unique(parts, storage)
            _append_unique(parts, ram)
        if include_color:
            _append_unique(parts, color)
        return " ".join(parts).strip()

    queries = [
        make_query(include_color=True, include_storage=True),
        make_query(include_color=False, include_storage=True),
        make_query(include_color=False, include_storage=False),
    ]
    queries = list(dict.fromkeys(query for query in queries if query))
    if not queries:
        raise ValueError(
            f"AmazonProduct {amazon_product.asin} has no searchable identity."
        )
    return queries


def build_flipkart_search_query(amazon_product) -> str:
    """Return the preferred concise Flipkart query."""
    return build_flipkart_search_queries(amazon_product)[0]


def _extract_pid(url: str) -> str | None:
    parsed = urlparse(url)
    pid_values = parse_qs(parsed.query).get("pid")
    if pid_values and pid_values[0].strip():
        return pid_values[0].strip().upper()
    match = PID_PATTERN.search(url)
    return match.group(1).upper() if match else None


def _normalise_product_url(href: str | None) -> str | None:
    if not href:
        return None
    url = urljoin(FLIPKART_BASE_URL, href)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"flipkart.com", "www.flipkart.com"}:
        return None
    return url


def _scrape_search_results(query: str) -> list[dict]:
    """Reuse the supplied Flipkart Playwright search behaviour."""
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
                SEARCH_URL.format(query=quote_plus(query)),
                wait_until="domcontentloaded",
                timeout=90000,
            )
            page.wait_for_timeout(6000)
            page.wait_for_selector(
                "div.cPHDOP.col-12-12, div[data-id]",
                timeout=30000,
            )
            for _ in range(5):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(800)

            pid_links = page.locator("a[href*='pid=']")
            if pid_links.count() > 0:
                rows = []
                position = 1
                for index in range(pid_links.count()):
                    link = pid_links.nth(index)
                    href = link.get_attribute("href")
                    lines = [
                        " ".join(line.split())
                        for line in link.inner_text().splitlines()
                        if line.strip()
                    ]
                    title = next(
                        (
                            line for line in lines
                            if line.lower() not in {
                                "add to compare",
                                "currently unavailable",
                            }
                            and "ratings" not in line.lower()
                            and "reviews" not in line.lower()
                            and not line.startswith("₹")
                        ),
                        "",
                    )
                    if not href or not title:
                        continue
                    rows.append(
                        {
                            "title": title,
                            "product_url": href,
                            "position": position,
                            "sponsored": "sponsored" in link.inner_text().lower(),
                        }
                    )
                    position += 1
                return rows

            products = page.locator("div[data-id]")
            if products.count() == 0:
                products = page.locator("div.cPHDOP.col-12-12")
            rows = []
            position = 1
            for index in range(products.count()):
                item = products.nth(index)
                title_locator = item.locator(
                    "a.WKTcLC, div.KzDlHZ, a.s1Q9rs"
                )
                if title_locator.count() == 0:
                    continue
                link_locator = item.locator(
                    "a.VJA3rP, a.CGtC98, a.s1Q9rs"
                )
                href = (
                    link_locator.first.get_attribute("href")
                    if link_locator.count()
                    else None
                )
                rows.append(
                    {
                        "title": title_locator.first.inner_text().strip(),
                        "product_url": href,
                        "position": position,
                        "sponsored": "sponsored" in item.inner_text().lower(),
                    }
                )
                position += 1
            return rows
        finally:
            if context:
                context.close()
            if browser:
                browser.close()


def search_flipkart(query: str) -> list[dict]:
    """Search Flipkart and return unique, normalized candidate dictionaries."""
    if not isinstance(query, str) or not query.strip():
        raise ValidationError("Flipkart search query cannot be empty.")

    logger.info("Fetching Flipkart webpage details securely via Playwright...")
    logger.info("Flipkart search URL: %s", SEARCH_URL.format(query=quote_plus(query.strip())))
    normalized = []
    seen_pids = set()
    for result in _scrape_search_results(query.strip()):
        product_url = _normalise_product_url(result.get("product_url"))
        pid = _extract_pid(product_url) if product_url else None
        title = (result.get("title") or "").strip()
        if not pid or not product_url or not title or pid in seen_pids:
            continue
        seen_pids.add(pid)
        normalized.append(
            {
                "pid": pid,
                "title": title,
                "product_url": product_url,
                "position": result.get("position") or len(normalized) + 1,
                "sponsored": bool(result.get("sponsored", False)),
            }
        )
    return normalized
