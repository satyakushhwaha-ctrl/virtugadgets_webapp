import json
import os
import time
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright


# 1. Define Pydantic Schema
class SellingPriceRange(BaseModel):
  min: int | None = Field(None, description="Lowest price observed as integer")
  max: int | None = Field(
      None, description="Highest selling price observed as integer"
  )


class Pricing(BaseModel):
  mrp_inr: int | None = Field(
      None, description="Maximum Retail Price (List Price) as integer"
  )
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
  seller_rating: float | None = Field(
      None, description="Seller rating out of 5 as float"
  )
  tenure_on_flipkart_years: int | None = None


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


class AmazonProductSchema(BaseModel):
  brand: str | None = Field(None, description="Brand name, e.g., ASUS, Dell")
  product_title: str | None = None
  url: str
  pid: str | None = Field(
      None, description="ASIN code extracted from URL, e.g. B0FC2M5CGL"
  )
  availability: str | None = Field(None, description="'In Stock' or 'Out of Stock'")
  images: list[str] = Field(
      default_factory=list, description="List of product image URLs"
  )
  pricing: Pricing
  seller_info: SellerInfo
  specifications: Specifications
  design_and_build: DesignAndBuild
  extras: Extras


def fetch_amazon_page_data_with_playwright(url: str) -> dict:
  """Robust Amazon scraper capturing text block hints and high-res image URLs."""
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
      page.goto(url, timeout=45000, wait_until="domcontentloaded")
      page.wait_for_timeout(4000)

      for _ in range(5):
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(400)

      extracted_data = page.evaluate("""() => {
                let getCleanText = (selector) => {
                    let el = document.querySelector(selector);
                    return el ? el.innerText.trim() : null;
                };

                let images = [];
                let imgEls = document.querySelectorAll('#altImages img, #imageBlock img, .imgTagWrapper img');
                imgEls.forEach(img => {
                    let src = img.src;
                    if (src && !src.includes('transparent-pixel') && !images.includes(src)) {
                        let highResSrc = src.replace(/\\._AC_US40_.*\\./, '.');
                        images.push(highResSrc);
                    }
                });

                let landingImage = document.querySelector('#landingImage');
                if (landingImage && landingImage.src) {
                    images.unshift(landingImage.src);
                }

                return {
                    pricingBlock: getCleanText('#corePriceDisplay_desktop_feature_div') || getCleanText('#desktop_buybox'),
                    mrpText: getCleanText('.basisPrice') || getCleanText('.a-text-price'),
                    sellingPriceText: getCleanText('.apexPriceToPay') || getCleanText('.priceToPay'),
                    sellerText: getCleanText('#sellerProfileTriggerId') || getCleanText('#merchant-info'),
                    fullBodyText: document.body.innerText,
                    imageUrls: [...new Set(images)].slice(0, 5)
                };
            }""")

      browser.close()
      return extracted_data

    except Exception as e:
      print(f"Playwright fetch warning: {e}")
      browser.close()
      return {
          "pricingBlock": "",
          "mrpText": "",
          "sellingPriceText": "",
          "sellerText": "",
          "fullBodyText": "",
          "imageUrls": [],
      }


def extract_amazon_product_data(url: str) -> dict:
  client = genai.Client()

  print("Fetching Amazon webpage details securely via Playwright...")
  page_data = fetch_amazon_page_data_with_playwright(url)

  combined_payload = (
      f"--- TARGETED PRICING BLOCK ---\n{page_data['pricingBlock']}\n\n"
      f"--- SPECIFIC MRP HINT ---\n{page_data['mrpText']}\n\n"
      f"--- SPECIFIC SELLING PRICE HINT ---\n{page_data['sellingPriceText']}\n\n"
      f"--- SELLER HINT ---\n{page_data['sellerText']}\n\n"
      f"--- EXTRACTED IMAGE URLS ---\n{json.dumps(page_data['imageUrls'])}\n\n"
      f"--- FULL PAGE TEXT ---\n{page_data['fullBodyText'][:22000]}"
  )

  prompt = f"""
    Analyze the provided Amazon.in product text content, targeted hints, pre-extracted image URLs, and URL to extract core product details accurately matching the requested schema.
    Target URL: {url}
    
    Instructions:
    - Extract ASIN from URL into 'pid'.
    - Populate 'images' with the high-resolution image URLs provided in the EXTRACTED IMAGE URLS section.
    - Parse clean integers for mrp_inr and current_selling_price_inr. Compute discount_percentage.
    - Set selling_price_range_inr with min as current selling price and max as MRP.
    
    Content:
    {combined_payload}
    """

  models_to_try = ["gemini-3.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
  max_retries = 3

  for model_name in models_to_try:
    for attempt in range(max_retries):
      try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AmazonProductSchema,
                temperature=0.0,
            ),
        )
        return json.loads(response.text)
      except Exception as e:
        print(
            f"Model {model_name} attempt {attempt + 1} encountered server load:"
            f" {e}"
        )
        if attempt < max_retries - 1:
          sleep_time = (attempt + 1) * 3
          print(f"Retrying in {sleep_time} seconds...")
          time.sleep(sleep_time)
        else:
          print(f"Switching to next available model...")

  raise Exception(
      "All Gemini models are temporarily busy. Please run the script again in"
      " a moment."
  )


if __name__ == "__main__":
  target_url = input("Enter Amazon.in Product URL: ").strip()

  print("\nExtracting data using Gemini API...")
  result_json = extract_amazon_product_data(target_url)

  print(json.dumps(result_json, indent=2))