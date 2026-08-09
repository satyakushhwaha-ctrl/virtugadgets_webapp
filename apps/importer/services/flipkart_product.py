import json
import os
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright


# 1. Pydantic Schema supporting images
class SellingPriceRange(BaseModel):
  min: int | None = Field(None, description="Lowest price observed as integer")
  max: int | None = Field(
      None, description="Highest selling price observed as integer"
  )


class Pricing(BaseModel):
  mrp_inr: int | None = Field(None, description="Maximum Retail Price as integer")
  current_selling_price_inr: int | None = Field(
      None, description="Current selling price as integer"
  )
  selling_price_range_inr: SellingPriceRange | None = None
  discount_percentage: int | None = Field(
      None, description="Discount percentage as integer"
  )


class SellerInfo(BaseModel):
  primary_seller: str | None = Field(
      None, description="Name of the default seller"
  )
  seller_rating: float | None = Field(None, description="Seller rating out of 5")
  tenure_on_flipkart_years: int | None = Field(
      None, description="Years on platform or null"
  )


class Specifications(BaseModel):
  processor: str | None = None
  ram: str | None = None
  storage: str | None = None
  operating_system: str | None = None
  display_size: str | None = None
  resolution: str | None = None


class DesignAndBuild(BaseModel):
  color: str | None = None
  weight_kg: float | None = Field(None, description="Weight in kg as float")


class Extras(BaseModel):
  software: str | None = None
  warranty: str | None = None


class FlipkartProductSchema(BaseModel):
  brand: str | None = Field(
      None, description="Brand name of the product, e.g., HP, Apple, Dell"
  )
  product_title: str | None = Field(None, description="Full product title")
  url: str
  pid: str | None = Field(
      None, description="Product ID extracted from URL parameter 'pid'"
  )
  availability: str | None = Field(None, description="'In Stock' or 'Out of Stock'")
  images: list[str] = Field(
      default_factory=list, description="List of high-resolution product image URLs"
  )
  pricing: Pricing
  seller_info: SellerInfo
  specifications: Specifications
  design_and_build: DesignAndBuild
  extras: Extras


def fetch_flipkart_page_data_with_playwright(url: str) -> dict:
  """Robust Flipkart scraper capturing DOM details, text blocks, and gallery images."""
  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    page = context.new_page()

    try:
      page.add_init_script(
          "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
      )
      page.goto(url, timeout=45000, wait_until="networkidle")
      page.wait_for_timeout(3000)

      # Scroll down to load lazy specifications and image thumbnails
      for _ in range(4):
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(500)

      # Extract image URLs and contextual details from Flipkart's DOM structure
      extracted_data = page.evaluate("""() => {
                let images = [];
                // Target primary product images and thumbnail wrappers on Flipkart
                let imgEls = document.querySelectorAll('img._396cs4, img._2r_T1I, ._3kidJX img, img.DByuf4');
                imgEls.forEach(img => {
                    let src = img.src || img.getAttribute('data-src');
                    if (src && !images.includes(src)) {
                        // Upgrade thumbnail sizes to high resolution if matching standard Flipkart sizing rules
                        let highResSrc = src.replace(/\\/image\\/\\d+\\/\\d+\\//, '/image/832/832/');
                        images.push(highResSrc);
                    }
                });

                return {
                    imageUrls: [...new Set(images)].slice(0, 5), // Top 5 unique images
                    fullBodyText: document.body.innerText
                };
            }""")

      browser.close()
      return extracted_data

    except Exception as e:
      print(f"Playwright fetch warning: {e}")
      browser.close()
      return {"imageUrls": [], "fullBodyText": ""}


def extract_flipkart_product_data(url: str) -> dict:
  client = genai.Client()

  print("Fetching Flipkart webpage details securely via Playwright...")
  page_data = fetch_flipkart_page_data_with_playwright(url)

  combined_payload = (
      f"--- EXTRACTED IMAGE URLS ---\n{json.dumps(page_data['imageUrls'])}\n\n"
      f"--- FULL PAGE TEXT ---\n{page_data['fullBodyText'][:22000]}"
  )

  prompt = f"""
    Analyze the provided Flipkart product text content, pre-extracted image URLs, and URL to extract core product details accurately matching the requested schema.
    Target URL: {url}
    
    Instructions:
    - Extract PID from URL query parameters (e.g., 'pid=COM...') into 'pid'.
    - Populate 'images' with the high-resolution image URLs provided in the EXTRACTED IMAGE URLS section.
    - Extract MRP, current selling price, discount percentage, seller details, specifications, weight in kg, color, and warranty accurately.
    - Set selling_price_range_inr with min as current selling price and max as MRP.
    
    Content:
    {combined_payload}
    """

  response = client.models.generate_content(
      model="gemini-3.5-flash",
      contents=prompt,
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
          response_schema=FlipkartProductSchema,
          temperature=0.0,
      ),
  )

  return json.loads(response.text)


if __name__ == "__main__":
  target_url = input("Enter Flipkart Product URL: ").strip()

  print("\nExtracting data using Gemini API...")
  result_json = extract_flipkart_product_data(target_url)

  print(json.dumps(result_json, indent=2))