#!/usr/bin/env python3
"""
Flipkart product scraper - optimized V2

Usage:
    python flipkart_scraper.py "https://www.flipkart.com/<product-url>"

Design:
- JSON-LD is the primary source for product, price, rating, reviews,
  availability, shipping and primary images.
- Targeted DOM selectors are used only for fields that JSON-LD normally
  does not expose reliably: MRP, seller, quantity limit, delivery text,
  highlights and specifications.
- Never scans the entire page for arbitrary prices, which prevents
  similar-product prices from becoming min/max for the current product.
"""

import asyncio
from html import unescape
import json
import re
import sys
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def unique_list(items):
    seen = set()
    result = []

    for item in items:
        item = clean_text(item)
        if not item:
            continue

        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)

    return result


def parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None

    text = str(value).replace(",", "").strip()

    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None


def integer_or_float(value):
    if value is None:
        return None

    if float(value).is_integer():
        return int(value)

    return value


def normalize_price(value: Any) -> Optional[int]:
    number = parse_number(value)
    if number is None:
        return None

    return int(round(number))


def normalize_availability(value: Any) -> Optional[str]:
    if not value:
        return None

    value = str(value).lower()

    if "instock" in value:
        return "IN_STOCK"

    if "outofstock" in value:
        return "OUT_OF_STOCK"

    if "preorder" in value:
        return "PREORDER"

    if "limitedavailability" in value:
        return "LIMITED"

    return None


def extract_product_id(url: str) -> Optional[str]:
    # Flipkart product URLs commonly contain ?pid=COM...
    parsed = urlparse(url)

    query = parsed.query

    match = re.search(r"(?:^|&)pid=([^&]+)", query, re.I)
    if match:
        return match.group(1)

    # Fallback to /p/<product-id>
    match = re.search(r"/p/([^/?#]+)", parsed.path, re.I)
    if match:
        return match.group(1)

    return None


async def first_text(page: Page, selectors) -> Optional[str]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(await locator.nth(i).inner_text())
                if text:
                    return text
        except Exception:
            continue

    return None


async def first_attr(page: Page, selectors, attr: str) -> Optional[str]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                value = clean_text(
                    await locator.nth(i).get_attribute(attr)
                )
                if value:
                    return value
        except Exception:
            continue

    return None


# ---------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------

async def extract_json_ld(page: Page) -> list[dict]:
    scripts = await page.locator(
        'script[type="application/ld+json"]'
    ).all()

    result = []

    for script in scripts:
        try:
            raw = await script.text_content()

            if not raw:
                continue

            data = json.loads(raw)

            if isinstance(data, list):
                result.extend(
                    item for item in data
                    if isinstance(item, dict)
                )

            elif isinstance(data, dict):
                # Handle @graph
                graph = data.get("@graph")

                if isinstance(graph, list):
                    result.extend(
                        item for item in graph
                        if isinstance(item, dict)
                    )

                result.append(data)

        except Exception:
            continue

    return result


def find_product_json_ld(items: list[dict]) -> Optional[dict]:
    for item in items:
        item_type = item.get("@type")

        if isinstance(item_type, list):
            types = [str(x).lower() for x in item_type]
        else:
            types = [str(item_type).lower()]

        if "product" in types:
            return item

    return None


# ---------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------

async def extract_mrp(page: Page, selling_price: Optional[int]) -> Optional[int]:
    """
    Extract only the current product's struck-through/list price.

    Important:
    We deliberately do NOT scan the whole page for prices because
    Flipkart pages contain many similar-product prices.
    """

    selectors = [
        # Common current Flipkart price containers
        "div._3I9_wc",
        "div._3auQ3N",
        "div._30jeq3",
        "div._3_G1hK",
        "div._25b18c",
        "div._2p6lqe",
        # Struck-through price variants
        "div[class*='30jeq3']",
        "div[class*='3I9_wc']",
        "div[class*='3auQ3N']",
        "div[class*='pQwF4']",
        "div[class*='strike']",
        "div[class*='strik']",
    ]

    candidates = []

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 10)):
                text = clean_text(await locator.nth(i).inner_text())

                if not text:
                    continue

                price = normalize_price(text)

                if price is None:
                    continue

                # MRP should normally be >= selling price.
                if selling_price is not None and price < selling_price:
                    continue

                candidates.append(price)

        except Exception:
            continue

    candidates = list(dict.fromkeys(candidates))

    # Prefer the smallest valid price above selling price.
    # This avoids accidentally selecting a much larger similar-product price.
    if selling_price is not None:
        valid = [x for x in candidates if x >= selling_price]

        if valid:
            return min(valid)

    return min(candidates) if candidates else None


def calculate_discount(
    selling_price: Optional[int],
    mrp: Optional[int],
) -> Optional[int]:

    if not selling_price or not mrp:
        return None

    if mrp <= selling_price:
        return 0

    return int(round(
        ((mrp - selling_price) / mrp) * 100
    ))


# ---------------------------------------------------------------------
# Seller
# ---------------------------------------------------------------------

async def extract_seller(page: Page) -> dict:
    seller_name = None
    seller_id = None

    # Target seller/merchant information instead of the entire page.
    selectors = [
        "#sellerName",
        "a[href*='/seller/']",
        "a[href*='seller']",
        "div[class*='seller'] a",
        "span[class*='seller']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 10)):
                element = locator.nth(i)

                text = clean_text(await element.inner_text())

                if text:
                    # Ignore generic navigation labels.
                    lowered = text.lower()

                    if (
                        "see other sellers" not in lowered
                        and "become a seller" not in lowered
                        and len(text) <= 120
                    ):
                        seller_name = text
                        break

                href = await element.get_attribute("href")

                if href:
                    match = re.search(
                        r"(?:seller|sellerid)[=/]([^/?&#]+)",
                        href,
                        re.I
                    )

                    if match:
                        seller_id = match.group(1)

            if seller_name:
                break

        except Exception:
            continue

    # Seller id can sometimes be present in page source.
    if not seller_id:
        try:
            html = await page.content()

            patterns = [
                r'"sellerId"\s*:\s*"([^"]+)"',
                r'"sellerID"\s*:\s*"([^"]+)"',
                r'"seller_id"\s*:\s*"([^"]+)"',
            ]

            for pattern in patterns:
                match = re.search(pattern, html, re.I)

                if match:
                    seller_id = match.group(1)
                    break

        except Exception:
            pass

    return {
        "name": seller_name,
        "id": seller_id,
    }


# ---------------------------------------------------------------------
# Availability / quantity
# ---------------------------------------------------------------------

async def extract_availability(
    page: Page,
    product_ld: Optional[dict],
) -> dict:

    status = None
    quantity_limit = None

    if product_ld:
        offers = product_ld.get("offers")

        if isinstance(offers, list):
            offers = offers[0] if offers else None

        if isinstance(offers, dict):
            status = normalize_availability(
                offers.get("availability")
            )

    # DOM fallback
    if not status:
        body = clean_text(
            await page.locator("body").inner_text()
        )

        if body:
            lower = body.lower()

            if "out of stock" in lower:
                status = "OUT_OF_STOCK"
            elif "currently unavailable" in lower:
                status = "OUT_OF_STOCK"
            elif "in stock" in lower:
                status = "IN_STOCK"

    # Quantity selectors / text
    quantity_selectors = [
        "input[aria-label*='quantity' i]",
        "input[name*='quantity' i]",
        "select[aria-label*='quantity' i]",
        "[class*='quantity']",
    ]

    for selector in quantity_selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                element = locator.nth(i)

                value = (
                    await element.get_attribute("value")
                    or await element.get_attribute("max")
                    or await element.get_attribute("aria-label")
                )

                number = parse_number(value)

                if number and 0 < number <= 100:
                    quantity_limit = int(number)
                    break

            if quantity_limit:
                break

        except Exception:
            continue

    return {
        "status": status,
        "quantity_limit": quantity_limit,
    }


# ---------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------

async def extract_delivery(
    page: Page,
    product_ld: Optional[dict],
) -> dict:

    free = None
    text = None

    # JSON-LD shipping fallback
    if product_ld:
        offers = product_ld.get("offers")

        if isinstance(offers, list):
            offers = offers[0] if offers else None

        if isinstance(offers, dict):
            shipping = offers.get("shippingDetails")

            if isinstance(shipping, dict):
                shipping_rate = shipping.get("shippingRate")

                if isinstance(shipping_rate, dict):
                    value = parse_number(
                        shipping_rate.get("value")
                    )

                    if value is not None:
                        free = value == 0

    # Target delivery section.
    selectors = [
        "div[class*='delivery']",
        "span[class*='delivery']",
        "div[class*='Delivery']",
        "span[class*='Delivery']",
    ]

    candidates = []

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 15)):
                value = clean_text(
                    await locator.nth(i).inner_text()
                )

                if not value:
                    continue

                lowered = value.lower()

                if (
                    "delivery" in lowered
                    or "deliver by" in lowered
                    or "get it by" in lowered
                ):
                    # Avoid giant page sections.
                    if len(value) <= 300:
                        candidates.append(value)

        except Exception:
            continue

    candidates = unique_list(candidates)

    if candidates:
        # Prefer the shortest meaningful delivery statement.
        text = min(candidates, key=len)

        if "free" in text.lower():
            free = True

    return {
        "free": free,
        "text": text,
    }


# ---------------------------------------------------------------------
# Ratings / reviews
# ---------------------------------------------------------------------

def extract_ratings(product_ld: Optional[dict]) -> dict:
    if not product_ld:
        return {
            "average": None,
            "review_count": None,
            "rating_count": None,
        }

    aggregate = product_ld.get("aggregateRating")

    if not isinstance(aggregate, dict):
        return {
            "average": None,
            "review_count": None,
            "rating_count": None,
        }

    average = parse_number(
        aggregate.get("ratingValue")
    )

    review_count = parse_number(
        aggregate.get("reviewCount")
    )

    rating_count = parse_number(
        aggregate.get("ratingCount")
    )

    return {
        "average": integer_or_float(average),
        "review_count": int(review_count)
        if review_count is not None else None,
        "rating_count": int(rating_count)
        if rating_count is not None else None,
    }


def extract_reviews(product_ld: Optional[dict]) -> list[dict]:
    if not product_ld:
        return []

    reviews = product_ld.get("review")

    if not isinstance(reviews, list):
        return []

    result = []

    for review in reviews:

        if not isinstance(review, dict):
            continue

        author = review.get("author")

        if isinstance(author, dict):
            author = author.get("name")

        rating = review.get("reviewRating")

        if isinstance(rating, dict):
            rating = parse_number(
                rating.get("ratingValue")
            )

        result.append({
            "title": clean_text(review.get("name")),
            "author": clean_text(author),
            "date": clean_text(
                review.get("datePublished")
            ),
            "body": clean_text(
                review.get("reviewBody")
            ),
            "rating": integer_or_float(rating),
        })

    return result


# ---------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------

def extract_images(product_ld: Optional[dict]) -> list[str]:
    if not product_ld:
        return []

    images = product_ld.get("image")

    if isinstance(images, str):
        images = [images]

    if not isinstance(images, list):
        return []

    result = []

    for image in images:

        if not isinstance(image, str):
            continue

        image = image.strip()

        # Keep actual product images.
        if not image.startswith(("http://", "https://")):
            continue

        lowered = image.lower()

        if any(
            x in lowered
            for x in [
                ".svg",
                "/promos/",
                "/internalised/",
                "batman-returns",
                "youtube",
                "instagram",
                "sell-image",
                "advertise-image",
                "gift-cards-image",
                "help-centre-image",
                "placeholder",
                "captcha",
                "robot-check",
                "loader",
            ]
        ):
            continue

        result.append(image)

    return unique_list(result)


# ---------------------------------------------------------------------
# Highlights
# ---------------------------------------------------------------------

async def extract_highlights(page: Page) -> list[str]:
    """
    Target product-highlight areas only.
    Avoids footer/header/navigation text.
    """

    selectors = [
        "div:has-text('Product Highlights')",
        "div:has-text('Highlights')",
        "section:has-text('Highlights')",
    ]

    candidates = []

    for selector in selectors:

        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 10)):

                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                if not text or len(text) > 2500:
                    continue

                # Split common bullet/newline formats.
                parts = re.split(
                    r"\n+|•|\u2022",
                    text
                )

                for part in parts:
                    part = clean_text(part)

                    if not part:
                        continue

                    lowered = part.lower()

                    # Avoid page navigation labels.
                    if lowered in {
                        "highlights",
                        "all details",
                        "show more",
                        "show less",
                    }:
                        continue

                    if len(part) <= 400:
                        candidates.append(part)

        except Exception:
            continue

    return unique_list(candidates)[:20]


# ---------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------

async def extract_specifications(page: Page) -> dict:
    """
    Extract key/value specification rows without scanning the entire page.

    Flipkart markup changes frequently, so multiple targeted strategies
    are used.
    """

    result = {}

    # Strategy 1: table rows
    try:
        rows = page.locator("tr")
        count = await rows.count()

        for i in range(min(count, 300)):
            row = rows.nth(i)

            cells = row.locator("th, td")
            cell_count = await cells.count()

            if cell_count < 2:
                continue

            values = []

            for j in range(min(cell_count, 4)):
                value = clean_text(
                    await cells.nth(j).inner_text()
                )

                if value:
                    values.append(value)

            if len(values) >= 2:
                key = values[0]
                value = " ".join(values[1:])

                if (
                    1 <= len(key) <= 120
                    and 1 <= len(value) <= 500
                    and key.lower() not in {
                        "specifications",
                        "general",
                        "dimensions",
                    }
                ):
                    result.setdefault(key, value)

    except Exception:
        pass

    # Strategy 2: common Flipkart specification blocks.
    # Find containers where the text is short enough to represent a row.
    selectors = [
        "div[class*='specification']",
        "div[class*='Specification']",
        "div[class*='attribute']",
        "div[class*='Attribute']",
    ]

    for selector in selectors:

        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 300)):

                container = locator.nth(i)

                children = container.locator(
                    ":scope > div, :scope > span"
                )

                child_count = await children.count()

                if child_count < 2:
                    continue

                values = []

                for j in range(min(child_count, 4)):
                    value = clean_text(
                        await children.nth(j).inner_text()
                    )

                    if value:
                        values.append(value)

                if len(values) >= 2:

                    key = values[0]
                    value = " ".join(values[1:])

                    if (
                        1 <= len(key) <= 120
                        and 1 <= len(value) <= 500
                    ):
                        result.setdefault(key, value)

        except Exception:
            continue

    return result


# ---------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------

async def scrape_flipkart(url: str) -> dict:

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        print(f"Opening: {url}")

        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        status_code = (
            response.status
            if response is not None
            else None
        )

        print(f"HTTP status: {status_code}")

        # Give Flipkart a short time to hydrate product sections.
        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=10000,
            )
        except Exception:
            pass

        # Extract JSON-LD first.
        json_ld_items = await extract_json_ld(page)
        product_ld = find_product_json_ld(
            json_ld_items
        )

        if not product_ld:
            await browser.close()

            raise RuntimeError(
                "Flipkart Product JSON-LD was not found. "
                "The page may have changed or returned a bot/challenge page."
            )

        # -------------------------------------------------------------
        # Product
        # -------------------------------------------------------------

        product_id = (
            clean_text(product_ld.get("sku"))
            or extract_product_id(url)
        )

        brand = product_ld.get("brand")

        if isinstance(brand, dict):
            brand = brand.get("name")

        title = clean_text(
            product_ld.get("name")
        )

        description = clean_text(
            product_ld.get("description")
        )

        # -------------------------------------------------------------
        # Pricing
        # -------------------------------------------------------------

        offers = product_ld.get("offers")

        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        if not isinstance(offers, dict):
            offers = {}

        selling_price = normalize_price(
            offers.get("price")
        )

        if selling_price is None:
            # Target current price, not every price on the page.
            price_text = await first_text(
                page,
                [
                    "div._30jeq3",
                    "div[class*='30jeq3']",
                ],
            )

            selling_price = normalize_price(
                price_text
            )

        mrp = await extract_mrp(
            page,
            selling_price,
        )

        discount = calculate_discount(
            selling_price,
            mrp,
        )

        # Current product has one current offer unless we explicitly
        # scrape seller offers. Therefore min/max must NOT use
        # similar-product prices.
        min_price = selling_price
        max_price = selling_price

        # -------------------------------------------------------------
        # Other fields
        # -------------------------------------------------------------

        seller = await extract_seller(page)

        availability = await extract_availability(
            page,
            product_ld,
        )

        delivery = await extract_delivery(
            page,
            product_ld,
        )

        ratings = extract_ratings(
            product_ld
        )

        reviews = extract_reviews(
            product_ld
        )

        images = extract_images(
            product_ld
        )

        highlights = await extract_highlights(
            page
        )

        specifications = await extract_specifications(
            page
        )

        currency = (
            offers.get("priceCurrency")
            or "INR"
        )

        # -------------------------------------------------------------
        # Final normalized schema
        # -------------------------------------------------------------

        result = {
            "marketplace": "flipkart",
            "url": url,

            "product": {
                "pid": product_id,
                "sku": product_id,
                "title": title,
                "brand": clean_text(brand),
                "description": description,
            },

            "pricing": {
                "currency": currency,
                "selling_price": selling_price,
                "mrp": mrp,
                "discount_percentage": discount,
                "min_price": min_price,
                "max_price": max_price,
            },

            "seller": {
                "name": seller.get("name"),
                "id": seller.get("id"),
            },

            "availability": availability,

            "delivery": delivery,

            "ratings": ratings,

            "images": images,

            "highlights": highlights,

            "specifications": specifications,

            "reviews": reviews,

            "raw": {
                "json_ld": json_ld_items,
            },
        }

        await browser.close()

        return result


class _FlipkartHTMLDocumentParser(HTMLParser):
    """Collect JSON-LD, visible text, and specification table rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld = []
        self._script_type = ""
        self._script_text = []
        self.body_text = []
        self.table_rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "ld+json" in attrs.get("type", "").lower():
            self._script_type = "ld+json"
            self._script_text = []
        if tag == "tr":
            self._row = []
        if tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._script_type:
            self._script_text.append(data)
        if data.strip():
            self.body_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script_type:
            try:
                value = json.loads(unescape("".join(self._script_text)))
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, dict):
                        graph = item.get("@graph")
                        self.jsonld.extend(graph if isinstance(graph, list) else [item])
            except (TypeError, ValueError):
                pass
            self._script_type = ""
            self._script_text = []
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            value = re.sub(r"\s+", " ", " ".join(self._cell)).strip()
            if value:
                self._row.append(value)
            self._cell = None
        if tag == "tr" and self._row is not None:
            if len(self._row) >= 2:
                self.table_rows.append(self._row)
            self._row = None


def parse_flipkart_product_html(raw_html: str, url: str) -> dict:
    """Adapt Scrape.do HTML to the existing Flipkart product result shape."""
    document = _FlipkartHTMLDocumentParser()
    document.feed(raw_html or "")
    product_ld = find_product_json_ld(document.jsonld)
    if not product_ld:
        raise RuntimeError(
            "Flipkart Product JSON-LD was not found. The page may have changed "
            "or returned a bot/challenge page."
        )

    product_id = clean_text(product_ld.get("sku")) or extract_product_id(url)
    brand = product_ld.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    offers = product_ld.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    offers = offers if isinstance(offers, dict) else {}
    selling_price = normalize_price(offers.get("price"))
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_html or "", flags=re.I | re.S)
    body_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()

    mrp = None
    mrp_match = re.search(
        r"(?:30jeq3|3I9_wc|3auQ3N|strike|strik)[^>]*>\s*(?:<[^>]+>\s*)*₹?\s*([\d,]+)",
        raw_html or "", re.I,
    )
    if mrp_match:
        candidate = normalize_price(mrp_match.group(1))
        if candidate is not None and (selling_price is None or candidate >= selling_price):
            mrp = candidate
    if mrp is None:
        prices = [normalize_price(value) for value in re.findall(r"₹\s*([\d,]+)", body_text)]
        prices = [value for value in prices if value is not None and (selling_price is None or value >= selling_price)]
        mrp = min(prices) if prices else None

    specifications = {}
    for row in document.table_rows:
        key, value = row[0], " ".join(row[1:])
        if key.lower() not in {"specifications", "general", "dimensions"}:
            specifications.setdefault(key, value)
    additional = product_ld.get("additionalProperty")
    if isinstance(additional, list):
        for item in additional:
            if isinstance(item, dict) and item.get("name") and item.get("value"):
                specifications.setdefault(str(item["name"]), str(item["value"]))

    seller_match = re.search(r"(?:sellerName|seller-name)[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)", raw_html or "", re.I)
    seller = clean_text(seller_match.group(1)) if seller_match else None
    availability = normalize_availability(offers.get("availability"))
    if not availability:
        lower_body = body_text.lower()
        if "out of stock" in lower_body or "currently unavailable" in lower_body:
            availability = "OUT_OF_STOCK"
        elif "in stock" in lower_body:
            availability = "IN_STOCK"

    delivery_match = re.search(r"((?:free )?delivery[^<]{0,160})", body_text, re.I)
    delivery_text = clean_text(delivery_match.group(1)) if delivery_match else None
    ratings = extract_ratings(product_ld)
    return {
        "marketplace": "flipkart",
        "url": url,
        "product": {
            "pid": product_id,
            "sku": product_id,
            "title": clean_text(product_ld.get("name")),
            "brand": clean_text(brand),
            "description": clean_text(product_ld.get("description")),
        },
        "pricing": {
            "currency": offers.get("priceCurrency") or "INR",
            "selling_price": selling_price,
            "mrp": mrp,
            "discount_percentage": calculate_discount(selling_price, mrp),
            "min_price": selling_price,
            "max_price": selling_price,
        },
        "seller": {"name": seller, "id": None},
        "availability": {"status": availability, "quantity_limit": None},
        "delivery": {"free": "free" in (delivery_text or "").lower() or None, "text": delivery_text},
        "ratings": ratings,
        "images": extract_images(product_ld),
        "highlights": [],
        "specifications": specifications,
        "reviews": extract_reviews(product_ld),
        "raw": {"json_ld": document.jsonld},
    }


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

async def main():

    if len(sys.argv) < 2:
        print(
            'Usage: python flipkart_scraper.py "FLIPKART_URL"'
        )
        sys.exit(1)

    url = sys.argv[1].strip()

    if "flipkart.com" not in url.lower():
        print("Error: Please provide a Flipkart product URL.")
        sys.exit(1)

    try:
        result = await scrape_flipkart(url)

        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            )
        )

    except Exception as exc:
        print(
            f"ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
