import asyncio
import json
import re
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright


# ============================================================
# HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def clean_price(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)

    value = str(value).strip()

    # Handle Amazon formats such as:
    # ₹1,29,200
    # 1,29,200
    # ₹225,000.00
    value = re.sub(r"[^\d.]", "", value)

    if not value:
        return None

    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def first_value(*values):
    for value in values:
        value = clean_text(value)
        if value:
            return value
    return None


def unique_list(values):
    result = []

    for value in values:
        if not value:
            continue

        value = clean_text(value)

        if value and value not in result:
            result.append(value)

    return result


async def get_text(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                if text:
                    return text

        except Exception:
            continue

    return None


async def get_attribute(page, selectors, attribute):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 10)):
                value = await locator.nth(i).get_attribute(attribute)

                if value:
                    return clean_text(value)

        except Exception:
            continue

    return None


# ============================================================
# JSON-LD
# ============================================================

async def extract_json_ld(page):
    results = []

    scripts = page.locator('script[type="application/ld+json"]')
    count = await scripts.count()

    for i in range(count):
        try:
            content = await scripts.nth(i).text_content()

            if not content:
                continue

            data = json.loads(content)

            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)

        except Exception:
            continue

    return results


def find_product_json_ld(items):
    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("@type")

        if item_type == "Product":
            return item

        if isinstance(item_type, list) and "Product" in item_type:
            return item

    return {}


# ============================================================
# ASIN
# ============================================================

def extract_asin(url, html):
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r'"asin"\s*:\s*"([A-Z0-9]{10})"',
        r'data-asin=["\']([A-Z0-9]{10})["\']',
        r'"ASIN"\s*:\s*"([A-Z0-9]{10})"',
    ]

    for pattern in patterns:
        match = re.search(pattern, url if "http" in pattern else html, re.I)

        if match:
            return match.group(1).upper()

    # URL patterns are more reliable, so try them explicitly.
    for pattern in patterns[:2]:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).upper()

    for pattern in patterns[2:]:
        match = re.search(pattern, html, re.I)
        if match:
            return match.group(1).upper()

    return None


# ============================================================
# PRICE
# ============================================================

async def extract_selling_price(page, product_ld=None):
    """
    IMPORTANT:
    Amazon has many prices on a product page.
    We specifically target the current Buy Box / PriceToPay
    and do NOT scan the entire page for prices.
    """

    # 1. Current Amazon PriceToPay / Buy Box
    selectors = [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-price-whole",
        "#corePriceDisplay_mobile_feature_div .priceToPay .a-price-whole",

        "#buybox .priceToPay .a-offscreen",
        "#buybox .priceToPay .a-price-whole",

        "#buyBoxAccordion .priceToPay .a-offscreen",
        "#buyBoxAccordion .priceToPay .a-price-whole",

        "#apex_desktop .priceToPay .a-offscreen",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                price = clean_price(text)

                if price is not None:
                    return price

        except Exception:
            continue

    # 2. Use the main price container only.
    # Exclude .a-text-price because that normally represents MRP/list price.
    try:
        container = page.locator(
            "#corePriceDisplay_desktop_feature_div"
        )

        if await container.count():
            locator = container.locator(
                ".a-price:not(.a-text-price) .a-offscreen"
            )

            count = await locator.count()

            for i in range(min(count, 5)):
                price = clean_price(
                    await locator.nth(i).inner_text()
                )

                if price is not None:
                    return price

    except Exception:
        pass

    # 3. Mobile main price container.
    try:
        container = page.locator(
            "#corePriceDisplay_mobile_feature_div"
        )

        if await container.count():
            locator = container.locator(
                ".a-price:not(.a-text-price) .a-offscreen"
            )

            count = await locator.count()

            for i in range(min(count, 5)):
                price = clean_price(
                    await locator.nth(i).inner_text()
                )

                if price is not None:
                    return price

    except Exception:
        pass

    return None


async def extract_mrp(page):
    """
    Extract the strikethrough/list price only.
    Never use a generic .a-price selector for MRP.
    """

    selectors = [
        "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .basisPrice .a-offscreen",

        "#corePriceDisplay_desktop_feature_div .a-price.a-text-price .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .a-price.a-text-price .a-offscreen",

        "#corePriceDisplay_desktop_feature_div .listPrice .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .listPrice .a-offscreen",

        "#priceblock_listprice",
        "#listPrice",
    ]

    prices = []

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                price = clean_price(
                    await locator.nth(i).inner_text()
                )

                if price is not None:
                    prices.append(price)

        except Exception:
            continue

    if not prices:
        return None

    return max(prices)


async def extract_discount(page):
    selectors = [
        "#corePriceDisplay_desktop_feature_div .savingsPercentage",
        "#corePriceDisplay_mobile_feature_div .savingsPercentage",
        ".savingsPercentage",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                if not text:
                    continue

                match = re.search(r"(\d{1,3})\s*%", text)

                if match:
                    return int(match.group(1))

        except Exception:
            continue

    return None


# ============================================================
# PRODUCT
# ============================================================

async def extract_product(page, product_ld):
    title = first_value(
        product_ld.get("name"),
        await get_text(page, ["#productTitle"]),
    )

    brand = None

    ld_brand = product_ld.get("brand")

    if isinstance(ld_brand, dict):
        brand = clean_text(ld_brand.get("name"))
    elif isinstance(ld_brand, str):
        brand = clean_text(ld_brand)

    description = clean_text(
        product_ld.get("description")
    )

    return {
        "title": title,
        "brand": brand,
        "description": description,
    }


# ============================================================
# SELLER
# ============================================================

async def extract_seller(page):
    seller_name = await get_text(
        page,
        [
            "#sellerProfileTriggerId",
            "#merchant-info a",
        ],
    )

    if seller_name:
        seller_name = re.split(
            r"\s+is\s+",
            seller_name,
            maxsplit=1,
            flags=re.I,
        )[0]

        seller_name = clean_text(seller_name)

    seller_id = None

    href = await get_attribute(
        page,
        [
            "#sellerProfileTriggerId",
            "#merchant-info a",
        ],
        "href",
    )

    if href:
        match = re.search(
            r"seller=([A-Z0-9]+)",
            href,
            re.I,
        )

        if match:
            seller_id = match.group(1).upper()

    # Fulfillment / shipping information
    merchant_text = await get_text(
        page,
        [
            "#merchant-info",
            "#fulfiller-info",
        ],
    )

    fulfilled_by = None
    ships_from = None

    if merchant_text:
        lower = merchant_text.lower()

        if "fulfilled by amazon" in lower or "amazon fulfilled" in lower:
            fulfilled_by = "Amazon"

        if "ships from amazon" in lower:
            ships_from = "Amazon"

    return {
        "name": seller_name,
        "id": seller_id,
        "fulfilled_by": fulfilled_by,
        "ships_from": ships_from,
    }


# ============================================================
# AVAILABILITY
# ============================================================

async def extract_stock(page, product_ld):
    # JSON-LD first
    offers = product_ld.get("offers")

    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    if isinstance(offers, dict):
        availability = str(
            offers.get("availability") or ""
        ).lower()

        if "instock" in availability:
            return "IN_STOCK"

        if "outofstock" in availability:
            return "OUT_OF_STOCK"

        if "preorder" in availability:
            return "PREORDER"

    text = await get_text(
        page,
        [
            "#availability",
            "#availability_feature_div",
        ],
    )

    if text:
        lower = text.lower()

        if "in stock" in lower:
            return "IN_STOCK"

        if "out of stock" in lower:
            return "OUT_OF_STOCK"

        if "unavailable" in lower:
            return "UNAVAILABLE"

    return None


async def extract_quantity_limit(page):
    selectors = [
        "#quantity",
        "#quantityRel",
        "select[name='quantity']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            if count == 0:
                continue

            element = locator.first

            tag = await element.evaluate(
                "(el) => el.tagName"
            )

            if tag == "SELECT":
                options = element.locator("option")
                option_count = await options.count()

                quantities = []

                for i in range(option_count):
                    value = await options.nth(i).get_attribute("value")

                    if value and value.isdigit():
                        quantities.append(int(value))

                if quantities:
                    return max(quantities)

            value = await element.get_attribute("value")

            if value and value.isdigit():
                return int(value)

        except Exception:
            continue

    return None


# ============================================================
# DELIVERY
# ============================================================

async def extract_delivery(page):
    selectors = [
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "#deliveryBlockMessage",
        "#deliveryBlockContainer",
        "#ddmDeliveryMessage",
        "#deliveryBlock",
    ]

    texts = []

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                if text:
                    texts.append(text)

        except Exception:
            continue

    texts = unique_list(texts)

    if not texts:
        return {
            "free": None,
            "date": None,
            "text": None,
        }

    text = " ".join(texts)

    return {
        "free": "free delivery" in text.lower(),
        "date": text,
        "text": text,
    }


# ============================================================
# RATINGS
# ============================================================

async def extract_rating(page, product_ld):
    try:
        aggregate = product_ld.get(
            "aggregateRating",
            {}
        )

        rating = aggregate.get("ratingValue")

        if rating is not None:
            return float(rating)

    except Exception:
        pass

    selectors = [
        "#acrPopover",
        "#averageCustomerReviews .a-icon-alt",
        "[data-hook='rating-out-of-text']",
        ".reviewCountTextLinkedHistogram .a-icon-alt",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                if not text:
                    text = clean_text(
                        await locator.nth(i).get_attribute("title")
                    )

                if not text:
                    continue

                match = re.search(
                    r"([0-5](?:\.[0-9])?)",
                    text,
                )

                if match:
                    return float(match.group(1))

        except Exception:
            continue

    return None


async def extract_review_count(page, product_ld):
    try:
        aggregate = product_ld.get(
            "aggregateRating",
            {}
        )

        value = aggregate.get("reviewCount")

        if value is not None:
            return int(value)

    except Exception:
        pass

    selectors = [
        "#acrCustomerReviewText",
        "#averageCustomerReviews #acrCustomerReviewText",
        "[data-hook='total-review-count']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(min(count, 5)):
                text = clean_text(
                    await locator.nth(i).inner_text()
                )

                if not text:
                    continue

                match = re.search(
                    r"([\d,]+)",
                    text,
                )

                if match:
                    return int(
                        match.group(1).replace(",", "")
                    )

        except Exception:
            continue

    return None


# ============================================================
# IMAGES
# ============================================================

def upgrade_amazon_image(url):
    if not url:
        return None

    # Convert:
    # image._SS40_.jpg
    # image._SX679_.jpg
    # image._AC_US40_.jpg
    # into:
    # image.jpg
    url = re.sub(
        r"\._[^.]+_\.",
        ".",
        url,
    )

    return url


async def extract_images(page, product_ld):
    images = []

    # JSON-LD first
    ld_images = product_ld.get("image", [])

    if isinstance(ld_images, str):
        ld_images = [ld_images]

    if isinstance(ld_images, list):
        for image in ld_images:
            if isinstance(image, str):
                images.append(image)

    # DOM fallback
    selectors = [
        "#altImages img",
        "#imageBlock img",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(count):
                src = (
                    await locator.nth(i).get_attribute("data-old-hires")
                    or await locator.nth(i).get_attribute("src")
                )

                if not src:
                    continue

                if (
                    "images-amazon.com" in src
                    or "media-amazon.com" in src
                ):
                    images.append(src)

        except Exception:
            continue

    images = unique_list(images)

    return unique_list(
        upgrade_amazon_image(image)
        for image in images
    )


# ============================================================
# HIGHLIGHTS
# ============================================================

async def extract_highlights(page):
    highlights = []

    try:
        locator = page.locator(
            "#feature-bullets ul li"
        )

        count = await locator.count()

        for i in range(count):
            text = clean_text(
                await locator.nth(i).inner_text()
            )

            if text:
                highlights.append(text)

    except Exception:
        pass

    return unique_list(highlights)


# ============================================================
# SPECIFICATIONS
# ============================================================

async def extract_specifications(page):
    specifications = {}

    selectors = [
        "#productDetails_techSpec_section_1 tr",
        "#productDetails_detailBullets_sections1 tr",
        "#prodDetails tr",
    ]

    for selector in selectors:
        try:
            rows = page.locator(selector)
            count = await rows.count()

            for i in range(count):
                cells = rows.nth(i).locator("th, td")
                cell_count = await cells.count()

                if cell_count >= 2:
                    key = clean_text(
                        await cells.nth(0).inner_text()
                    )

                    value = clean_text(
                        await cells.nth(1).inner_text()
                    )

                    if key and value:
                        specifications[key] = value

        except Exception:
            continue

    # Detail bullets fallback
    try:
        bullets = page.locator(
            "#detailBullets_feature_div li"
        )

        count = await bullets.count()

        for i in range(count):
            text = clean_text(
                await bullets.nth(i).inner_text()
            )

            if not text or ":" not in text:
                continue

            key, value = text.split(":", 1)

            key = clean_text(key)
            value = clean_text(value)

            if key and value:
                specifications[key] = value

    except Exception:
        pass

    return specifications


# ============================================================
# RANKING
# ============================================================

def extract_ranking(specifications):
    value = specifications.get(
        "Best Sellers Rank"
    )

    if not value:
        return {}

    result = {}

    match = re.search(
        r"#([\d,]+)\s+in\s+Computers\s*&\s*Accessories",
        value,
        re.I,
    )

    if match:
        result["computers_accessories"] = int(
            match.group(1).replace(",", "")
        )

    match = re.search(
        r"#([\d,]+)\s+in\s+Traditional\s+Laptops",
        value,
        re.I,
    )

    if match:
        result["traditional_laptops"] = int(
            match.group(1).replace(",", "")
        )

    return result


# ============================================================
# REVIEWS
# ============================================================

async def extract_reviews(page):
    reviews = []

    try:
        review_locator = page.locator(
            "[data-hook='review']"
        )

        count = await review_locator.count()

        for i in range(min(count, 20)):
            review = review_locator.nth(i)

            try:
                title = clean_text(
                    await review.locator(
                        "[data-hook='review-title']"
                    ).inner_text()
                )
            except Exception:
                title = None

            try:
                body = clean_text(
                    await review.locator(
                        "[data-hook='review-body']"
                    ).inner_text()
                )
            except Exception:
                body = None

            try:
                rating_text = clean_text(
                    await review.locator(
                        "[data-hook='review-star-rating']"
                    ).inner_text()
                )
            except Exception:
                rating_text = None

            rating = None

            if rating_text:
                match = re.search(
                    r"([0-5](?:\.[0-9])?)",
                    rating_text,
                )

                if match:
                    rating = float(match.group(1))

            if title or body:
                reviews.append({
                    "title": title,
                    "content": body,
                    "rating": rating,
                })

    except Exception:
        pass

    return reviews


# ============================================================
# MAIN SCRAPER
# ============================================================

async def scrape_amazon(url):
    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
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

        await page.add_init_script(
            """
            Object.defineProperty(
                navigator,
                'webdriver',
                {
                    get: () => undefined
                }
            );
            """
        )

        print("Opening:", url)

        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        if response:
            print("HTTP status:", response.status)

        # Allow Amazon's dynamic price/product area to render.
        await page.wait_for_timeout(5000)

        for selector in [
            "#productTitle",
            "#dp",
            "#ppd",
        ]:
            try:
                await page.wait_for_selector(
                    selector,
                    timeout=10000,
                )
                break
            except Exception:
                continue

        await page.wait_for_timeout(2000)

        html = await page.content()

        # ====================================================
        # JSON-LD
        # ====================================================

        json_ld = await extract_json_ld(page)

        product_ld = find_product_json_ld(
            json_ld
        )

        # ====================================================
        # ASIN
        # ====================================================

        asin = extract_asin(
            url,
            html,
        )

        # ====================================================
        # PRODUCT
        # ====================================================

        product = await extract_product(
            page,
            product_ld,
        )

        # ====================================================
        # SPECIFICATIONS
        # ====================================================

        specifications = await extract_specifications(
            page
        )

        # Brand fallback
        if not product["brand"]:
            product["brand"] = first_value(
                specifications.get("Brand Name"),
                specifications.get("Brand"),
            )

        # ====================================================
        # PRICING
        # ====================================================

        # IMPORTANT:
        # Pass product_ld as the second argument.
        # This fixes the TypeError from the previous version.
        selling_price = await extract_selling_price(
            page,
            product_ld,
        )

        mrp = await extract_mrp(
            page
        )

        # Calculate discount from the actual selling price
        # and MRP whenever both are valid.
        discount_percentage = None

        if (
            selling_price is not None
            and mrp is not None
            and mrp > selling_price
        ):
            discount_percentage = round(
                (
                    (mrp - selling_price)
                    / mrp
                ) * 100
            )

        elif (
            selling_price is not None
            and mrp is not None
            and mrp == selling_price
        ):
            discount_percentage = 0

        # Sanity check
        if (
            selling_price is not None
            and mrp is not None
            and mrp < selling_price
        ):
            print(
                "WARNING: MRP is lower than selling price. "
                "Ignoring MRP."
            )

            mrp = None
            discount_percentage = None

        # We are on one product page, so min/max are the
        # current product price. Do not scan the whole page.
        min_price = selling_price
        max_price = selling_price

        # ====================================================
        # SELLER
        # ====================================================

        seller = await extract_seller(
            page
        )

        # ====================================================
        # AVAILABILITY
        # ====================================================

        stock_status = await extract_stock(
            page,
            product_ld,
        )

        quantity_limit = await extract_quantity_limit(
            page
        )

        # ====================================================
        # DELIVERY
        # ====================================================

        delivery = await extract_delivery(
            page
        )

        # ====================================================
        # RATINGS
        # ====================================================

        rating = await extract_rating(
            page,
            product_ld,
        )

        review_count = await extract_review_count(
            page,
            product_ld,
        )

        rating_count = None

        try:
            rating_count = int(
                product_ld
                .get("aggregateRating", {})
                .get("ratingCount")
            )
        except Exception:
            pass

        # ====================================================
        # IMAGES
        # ====================================================

        images = await extract_images(
            page,
            product_ld,
        )

        # ====================================================
        # HIGHLIGHTS
        # ====================================================

        highlights = await extract_highlights(
            page
        )

        # ====================================================
        # RANKING
        # ====================================================

        ranking = extract_ranking(
            specifications
        )

        # ====================================================
        # REVIEWS
        # ====================================================

        reviews = await extract_reviews(
            page
        )

        # ====================================================
        # FINAL RESULT
        # ====================================================

        result = {
            "marketplace": "amazon",

            "url": url,

            "product": {
                "id": asin,
                "asin": asin,
                "sku": asin,
                "title": product["title"],
                "brand": product["brand"],
                "description": product["description"],
            },

            "pricing": {
                "currency": "INR",
                "selling_price": selling_price,
                "mrp": mrp,
                "discount_percentage": discount_percentage,
                "min_price": min_price,
                "max_price": max_price,
            },

            "seller": seller,

            "availability": {
                "status": stock_status,
                "purchase_quantity_limit": quantity_limit,
            },

            "delivery": delivery,

            "ratings": {
                "average": rating,
                "review_count": review_count,
                "rating_count": rating_count,
            },

            "ranking": ranking,

            "images": images,

            "highlights": highlights,

            "specifications": specifications,

            "reviews": reviews,

            "raw": {
                "json_ld": json_ld,
            },
        }

        await browser.close()

        return result


# ============================================================
# CLI
# ============================================================

async def main():
    if len(sys.argv) < 2:
        print(
            '\nUsage:\n'
            'python amazon_scraper.py '
            '"https://www.amazon.in/dp/B0GYZXGXWG"\n'
        )
        sys.exit(1)

    url = sys.argv[1]

    result = await scrape_amazon(url)

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())