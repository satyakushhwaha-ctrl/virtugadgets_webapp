"""Flipkart candidate discovery service."""

import logging
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from django.conf import settings
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


class FlipkartSearchScrapingError(ConnectionError):
    """Raised when a fetched Flipkart search page is not usable."""


def build_flipkart_search_url(query: str, page: int = 1) -> str:
    """Build the same Flipkart search URL used by the legacy scraper."""
    url = SEARCH_URL.format(query=quote_plus(query.strip()))
    if int(page) > 1:
        url += f"&page={int(page)}"
    return url


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


QUERY_NOISE = {
    "and", "best", "black", "blue", "computer", "dts", "ever", "gamepass",
    "gaming", "home", "latest", "laptop", "latest", "new", "notebook", "smartphone",
    "office", "silver", "thin", "upgradeable", "with", "xbox", "core",
    "hs", "hx", "quad", "hexa", "ai", "rtx", "gtx", "rx", "arc", "graphics", "g", "mp",
    "camera", "battery", "long", "included", "pen", "smart", "s", "life",
}
KNOWN_BRANDS = {
    "acer", "apple", "asus", " dell", "dell", "hp", "lenovo", "lg", "mi",
    "msi", "oneplus", "realme", "samsung", "sony", "vivo", "xiaomi",
}


def _normalise_text(value: str | None) -> str:
    return " ".join(str(value or "").replace("/", " ").split())


def _compact_capacity(value: str | None) -> str | None:
    normalised = _normalise_capacity(value, "storage")
    if not normalised:
        return None
    return normalised.replace(" ", "")


def _normalise_title(title: str) -> str:
    return _normalise_text(title)


def _append_unique(parts: list[str], value: str | None) -> None:
    if not value:
        return
    existing = " ".join(parts).casefold()
    if not re.search(
        rf"(?<!\w){re.escape(value.casefold())}(?!\w)",
        existing,
    ):
        parts.append(value)


def _model_identifiers(title: str) -> list[str]:
    identifiers = []
    phone_model = re.search(
        r"\b(?:galaxy\s+)?(S\d{2}(?:\s+(?:Ultra|Edge|FE|Plus|Pro|Max|Mini|SE|Air|Fold|Flip))?)\b",
        title,
        re.I,
    )
    if phone_model:
        identifiers.append(phone_model.group(1))
    iphone = re.search(r"\biPhone\s+([0-9]+(?:\s+(?:Pro|Plus|Max))?)", title, re.I)
    if iphone:
        identifiers.append(iphone.group(1))
    for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?", title):
        compact = token.replace("-", "")
        if compact.casefold() in {"m365", "office24", "ddr5", "ddr4"}:
            continue
        if re.fullmatch(r"\d+-[A-Za-z]{2,}\d+[A-Za-z0-9]*", token):
            token = token.split("-", 1)[1]
            compact = token.replace("-", "")
        if re.fullmatch(r"(?:[A-Za-z]{2,}\d+[A-Za-z0-9]*|\d+[A-Za-z]+\d+[A-Za-z0-9]*)", compact):
            if compact.casefold() not in {"gb", "tb", "win11", "win10"}:
                identifiers.append(token)
    return list(dict.fromkeys(identifiers))


def _normalise_processor(value: str | None, title: str) -> str | None:
    source = f"{value or ''} {title}"
    match = re.search(r"ryzen\s*([3579])\s*(?:hexa\s*core\s*)?(\d{4,5}[A-Za-z]{0,3})", source, re.I)
    if match:
        return f"Ryzen {match.group(1)} {match.group(2).upper()}"
    match = re.search(r"core\s*(?:i\s*)?([3579])\s*(?:\d+(?:st|nd|rd|th)\s+gen\s*)?(\d{3,5}[A-Za-z]{0,3})", source, re.I)
    if match:
        return f"Core {match.group(1)} {match.group(2).upper()}"
    return None


def _normalise_gpu(title: str) -> str | None:
    match = re.search(r"\b(?:NVIDIA\s+)?(?:GeForce\s+)?(RTX|GTX|RX|ARC)\s*(\d{3,4})\b", title, re.I)
    return f"{match.group(1).upper()} {match.group(2)}" if match else None


def _family_tokens(title: str, brand: str, model: list[str]) -> list[str]:
    clean = re.sub(r"\([^)]*\)", " ", title)
    clean = re.sub(r"\d+(?:\.\d+)?\s*(?:GB|TB|RAM|SSD|HDD|KG|HZ|INCH|CM)\b", " ", clean, flags=re.I)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]*", clean)
    result = []
    for token in tokens:
        low = token.casefold()
        if (low in QUERY_NOISE or low.startswith(("office", "gamepass"))
                or low in {"phone", "phones", "tv", "television"}
                or low in {brand.casefold()} or token in model
                or any(low == part.casefold() for value in model for part in value.split())):
            continue
        if low in {"amd", "intel", "ryzen", "nvidia", "geforce", "windows", "home", "ssd", "ddr5", "ddr4"}:
            continue
        if re.fullmatch(r"\d+[A-Za-z]*", token):
            continue
        result.append(token)
        if len(result) >= 2:
            break
    return result


def _query_parts(amazon_product) -> dict[str, object]:
    title = _normalise_title(amazon_product.product_title)
    brand = _normalise_text(amazon_product.brand)
    if not brand:
        brand = next((token for token in title.split() if token.casefold() in KNOWN_BRANDS), "")
    models = _model_identifiers(title)
    family = _family_tokens(title, brand, models)
    processor = _normalise_processor(amazon_product.processor, title)
    gpu = _normalise_gpu(title)
    ram = _compact_capacity(amazon_product.ram)
    storage = _compact_capacity(amazon_product.storage)
    if not ram:
        ram_match = re.search(r"(\d+)\s*GB\s*(?:DDR\d+|RAM)", title, re.I)
        ram = f"{ram_match.group(1)}GB" if ram_match else None
    if not storage:
        storage_match = re.search(r"(\d+)\s*GB\s*(?:SSD|NVME|HDD|ROM|STORAGE)", title, re.I)
        storage = f"{storage_match.group(1)}GB" if storage_match else None
    display = re.search(r"\b\d+(?:\.\d+)?\s*(?:inch|inches|cm)\b", title, re.I)
    resolution = re.search(r"\b(?:4k|8k|1080p|1440p|full\s*hd|qhd|uhd)\b", title, re.I)
    panel = next((value for value in ("QLED", "OLED", "AMOLED", "IPS") if re.search(value, title, re.I)), None)
    category = "phone" if re.search(r"\b(?:iphone|phone|galaxy|pixel)\b", title, re.I) else ""
    if re.search(r"\b(?:tv|television|qled|oled)\b", title, re.I):
        category = "tv"
    color = _normalise_text(amazon_product.color) or None
    return {
        "brand": brand,
        "family": family,
        "models": models,
        "processor": processor,
        "gpu": gpu,
        "ram": ram,
        "storage": storage,
        "display": display.group(0) if display else None,
        "resolution": resolution.group(0) if resolution else None,
        "panel": panel,
        "color": color if category == "phone" else None,
        "category": category,
    }


def build_flipkart_search_queries(amazon_product) -> list[str]:
    """Build bounded, product-aware Flipkart queries from stored attributes."""
    parts = _query_parts(amazon_product)
    brand = parts["brand"]
    family = parts["family"]
    models = parts["models"]
    processor = parts["processor"]
    gpu = parts["gpu"]
    ram = parts["ram"]
    storage = parts["storage"]
    color = parts["color"]
    category = parts["category"]
    if category == "tv":
        specific = [brand, *models, parts["display"], parts["resolution"], parts["panel"]]
        processor_gpu = specific
        family_specs = specific
        model_only = [brand, *models]
    elif re.search(r"\b(?:headphones?|headset|earbuds?|wh-)\b", amazon_product.product_title, re.I):
        specific = [brand, *models]
        processor_gpu = specific
        family_specs = specific
        model_only = specific
    else:
        specific = [brand, *family, *models, processor, gpu, ram, storage, color]
        processor_gpu = [brand, *family, *models, processor, gpu]
        family_specs = [brand, *family, processor, ram, storage]
        model_only = [brand, *models]
    queries = [
        " ".join(str(part) for part in specific if part),
        " ".join(str(part) for part in processor_gpu if part),
        " ".join(str(part) for part in family_specs if part),
        " ".join(str(part) for part in model_only if part),
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
            if page:
                try:
                    page.close()
                except Exception:
                    logger.debug("Could not close Flipkart search page", exc_info=True)
            if context:
                context.close()
            if browser:
                browser.close()


class _FlipkartSearchHTMLParser(HTMLParser):
    """Small HTML adapter for the selectors used by the old search parser."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._anchor = None
        self._anchors = []
        self._page_numbers = set()
        self._next_page = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        href = attributes.get("href", "")
        classes = attributes.get("class", "")
        if tag == "a" and href:
            self._anchor = {"href": href, "text": [], "sponsored": False}
            self._anchors.append(self._anchor)
            if "page=" in href:
                match = re.search(r"[?&]page=(\d+)", href)
                if match:
                    self._page_numbers.add(int(match.group(1)))
            if "next" in f"{href} {classes}".lower():
                self._next_page = True

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._anchor = None


def _parse_flipkart_search_html(raw_html: str) -> dict:
    """Parse provider HTML into the same raw rows as the legacy parser."""
    parser = _FlipkartSearchHTMLParser()
    parser.feed(raw_html or "")
    rows = []
    seen_urls = set()
    for anchor in parser._anchors:
        product_url = _normalise_product_url(anchor["href"])
        if not product_url or not _extract_pid(product_url):
            continue
        lines = [" ".join(line.split()) for line in anchor["text"] if line.strip()]
        title = next(
            (
                line for line in lines
                if line.lower() not in {"add to compare", "currently unavailable"}
                and "ratings" not in line.lower()
                and "reviews" not in line.lower()
                and not line.startswith("₹")
            ),
            "",
        )
        if not title or product_url in seen_urls:
            continue
        seen_urls.add(product_url)
        rows.append({
            "title": title,
            "product_url": product_url,
            "position": len(rows) + 1,
            "sponsored": "sponsored" in " ".join(lines).lower(),
        })
    return {
        "results": rows,
        "has_next": parser._next_page or bool(parser._page_numbers and max(parser._page_numbers) > 1),
    }


def _flipkart_block_reason(raw_html: str) -> str | None:
    content = (raw_html or "").lower()
    markers = (
        ("captcha", "captcha page"),
        ("robot check", "robot-check page"),
        ("access denied", "access-denied page"),
        ("verify you are human", "human-verification page"),
        ("unusual traffic", "unusual-traffic page"),
    )
    for marker, reason in markers:
        if marker in content:
            return reason
    return None


def _scrapedo_flipkart_search_page(query: str, page: int) -> dict:
    from .scrapedo import ScrapeDoWebProvider

    target_url = build_flipkart_search_url(query, page)
    logger.info(
        "[FLIPKART SEARCH] provider=scrapedo_web keyword=%s page=%s",
        query, page,
    )
    response = ScrapeDoWebProvider().fetch(target_url, render=True)
    html = response.html or ""
    logger.info(
        "[FLIPKART SEARCH] status=%s html_length=%s request_cost=%s",
        response.status_code, len(html), response.request_cost or "unknown",
    )
    block_reason = _flipkart_block_reason(html)
    if block_reason:
        raise FlipkartSearchScrapingError(
            f"Scrape.do returned a Flipkart {block_reason}."
        )
    parsed = _parse_flipkart_search_html(html)
    if not parsed["results"]:
        empty_markers = ("no results", "no products found", "sorry, no")
        if not any(marker in html.lower() for marker in empty_markers):
            raise FlipkartSearchScrapingError(
                "Scrape.do returned HTML but Flipkart search candidates were not found."
            )
    logger.info(
        "[FLIPKART SEARCH] parser candidates=%s",
        len(parsed["results"]),
    )
    return {
        **parsed,
        "source": response.provider,
        "status_code": response.status_code,
        "request_cost": response.request_cost,
        "url": target_url,
    }


def _normalize_flipkart_candidates(raw_results: list[dict]) -> list[dict]:
    normalized = []
    seen_pids = set()
    for result in raw_results:
        product_url = _normalise_product_url(result.get("product_url"))
        pid = _extract_pid(product_url) if product_url else None
        title = (result.get("title") or "").strip()
        if not pid or not product_url or not title or pid in seen_pids:
            continue
        seen_pids.add(pid)
        normalized.append({
            "pid": pid,
            "title": title,
            "product_url": product_url,
            "position": result.get("position") or len(normalized) + 1,
            "sponsored": bool(result.get("sponsored", False)),
        })
    return normalized


def search_flipkart_page(query: str, page: int = 1) -> dict:
    """Fetch and normalize one page, preferring Scrape.do when configured."""
    if not isinstance(query, str) or not query.strip():
        raise ValidationError("Flipkart search query cannot be empty.")
    page = max(1, int(page))
    if getattr(settings, "SCRAPEDO_API_TOKEN", "").strip():
        payload = _scrapedo_flipkart_search_page(query.strip(), page)
        payload["results"] = _normalize_flipkart_candidates(payload["results"])
        return payload

    logger.warning(
        "[FLIPKART SEARCH] provider=scrapedo fallback=playwright reason=token_not_configured"
    )
    if page != 1:
        raise FlipkartSearchScrapingError(
            "Flipkart pagination requires SCRAPEDO_API_TOKEN."
        )
    return {
        "results": _normalize_flipkart_candidates(_scrape_search_results(query.strip())),
        "has_next": False,
        "source": "playwright",
        "request_cost": None,
        "url": build_flipkart_search_url(query, page),
    }


def search_flipkart(query: str, page: int = 1) -> list[dict]:
    """Search Flipkart and return unique, normalized candidate dictionaries."""
    if not isinstance(query, str) or not query.strip():
        raise ValidationError("Flipkart search query cannot be empty.")

    logger.info("[FLIPKART SEARCH] target_url=%s", build_flipkart_search_url(query, page))
    return search_flipkart_page(query, page)["results"]
