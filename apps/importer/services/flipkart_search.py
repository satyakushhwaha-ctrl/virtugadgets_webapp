import re
import time
import pandas as pd
from playwright.sync_api import sync_playwright


SEARCH_URL = "https://www.flipkart.com/search?q=laptop&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=on&as=off"


def scrape():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
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

        page.add_init_script("""
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});
""")

        print("Opening Flipkart...")

        page.goto(
            SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        page.wait_for_timeout(6000)

        print("Title:", page.title())

        page.screenshot(
            path="flipkart_search.png",
            full_page=True,
        )

        try:
            # Flipkart typically uses wrapper divs like 'div[class*="tUxRFH"]' or standard product containers 
            # Looking for general product card anchors/containers
            page.wait_for_selector(
                "div.cPHDOP.col-12-12, div[data-id]",
                timeout=30000,
            )
        except:
            print("Search results not found.")
            print("Screenshot saved as flipkart_search.png")
            browser.close()
            return

        for _ in range(5):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)

        # Flipkart product cards container selector
        products = page.locator("div.cPHDOP.col-12-12")

        total = products.count()

        print(f"Found {total} potential product blocks")

        rows = []
        position = 1

        for i in range(total):

            item = products.nth(i)

            try:
                # Check if this container actually holds a product title/link to filter out non-product rows
                title_locator = item.locator("a.WKTcLC, div.KzDlHZ, a.s1Q9rs")
                
                if title_locator.count() == 0:
                    continue

                title = title_locator.first.inner_text().strip()

                link_locator = item.locator("a.VJA3rP, a.CGtC98, a.s1Q9rs")
                href = (
                    link_locator.first.get_attribute("href")
                    if link_locator.count()
                    else None
                )

                url = None
                if href:
                    if href.startswith("http"):
                        url = href
                    else:
                        url = "https://www.flipkart.com" + href

                # Price extraction
                price_locator = item.locator("div.Nx9bqj")
                price = (
                    price_locator.first.inner_text().strip()
                    if price_locator.count()
                    else None
                )

                sponsored = False
                text = item.inner_text().lower()
                if "sponsored" in text:
                    sponsored = True

                rows.append(
                    {
                        "position": position,
                        "title": title,
                        "price": price,
                        "product_url": url,
                        "sponsored": sponsored,
                    }
                )
                position += 1

            except Exception as e:
                print(e)

        browser.close()

        df = pd.DataFrame(rows)

        df.to_excel(
            "flipkart_laptops.xlsx",
            index=False,
        )

        print(df.head())

        print()

        print("=" * 80)

        print(f"Saved {len(df)} products")

        print("Output: flipkart_laptops.xlsx")


if __name__ == "__main__":
    scrape()