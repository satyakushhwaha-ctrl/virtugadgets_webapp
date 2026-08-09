import re
import time
import pandas as pd
from playwright.sync_api import sync_playwright


SEARCH_URL = "https://www.amazon.in/s?k=laptops&i=computers&crid=1DRNXC0TNI385&sprefix=laptop%2Ccomputers%2C381&ref=nb_sb_noss_2"


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

        print("Opening Amazon...")

        page.goto(
            SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        page.wait_for_timeout(6000)

        print("Title:", page.title())

        page.screenshot(
            path="amazon_search.png",
            full_page=True,
        )

        try:
            page.wait_for_selector(
                "div[data-component-type='s-search-result']",
                timeout=30000,
            )
        except:
            print("Search results not found.")
            print("Screenshot saved as amazon_search.png")
            browser.close()
            return

        for _ in range(5):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)

        products = page.locator(
            "div[data-component-type='s-search-result']"
        )

        total = products.count()

        print(f"Found {total} products")

        rows = []

        for i in range(total):

            item = products.nth(i)

            try:

                asin = item.get_attribute("data-asin")

                title_locator = item.locator("h2 span")

                title = (
                    title_locator.first.inner_text().strip()
                    if title_locator.count()
                    else None
                )

                link_locator = item.locator("h2 a")

                href = (
                    link_locator.first.get_attribute("href")
                    if link_locator.count()
                    else None
                )

                url = None

                if href:
                    url = "https://www.amazon.in" + href.split("?")[0]

                sponsored = False

                text = item.inner_text().lower()

                if "sponsored" in text:
                    sponsored = True

                rows.append(
                    {
                        "position": i + 1,
                        "asin": asin,
                        "title": title,
                        "product_url": url,
                        "sponsored": sponsored,
                    }
                )

            except Exception as e:
                print(e)

        browser.close()

        df = pd.DataFrame(rows)

        df.to_excel(
            "amazon_laptops.xlsx",
            index=False,
        )

        print(df.head())

        print()

        print("=" * 80)

        print(f"Saved {len(df)} products")

        print("Output: amazon_laptops.xlsx")


if __name__ == "__main__":
    scrape()