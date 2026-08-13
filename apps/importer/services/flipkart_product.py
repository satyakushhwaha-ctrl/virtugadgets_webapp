import json
from urllib.parse import urlparse

try:
  from pydantic import BaseModel, Field
  PYDANTIC_AVAILABLE = True
except ModuleNotFoundError:
  PYDANTIC_AVAILABLE = False

  class BaseModel:
    pass

  def Field(default=None, **kwargs):
    return default

from django.db import transaction
from django.utils import timezone

from ..models import FlipkartProduct, FlipkartSearchResult, ImportStatus


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
  from playwright.sync_api import sync_playwright

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
  if not PYDANTIC_AVAILABLE:
    raise RuntimeError(
        "Flipkart product extraction requires the pydantic package."
    )

  from google import genai
  from google.genai import types

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


def extract_flipkart_product(url: str) -> dict:
  """Extract and normalize one Flipkart product without saving it."""
  parsed = urlparse(url or "")
  if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
      "flipkart.com",
      "www.flipkart.com",
  }:
    raise ValueError("Invalid Flipkart product URL.")

  raw_data = extract_flipkart_product_data(url)
  if not isinstance(raw_data, dict):
    raise ValueError("Flipkart product scraper returned invalid data.")

  pricing = raw_data.get("pricing") or {}
  price_range = pricing.get("selling_price_range_inr") or {}
  seller_info = raw_data.get("seller_info") or {}
  specifications = raw_data.get("specifications") or {}
  design_and_build = raw_data.get("design_and_build") or {}
  extras = raw_data.get("extras") or {}
  return {
      "pid": raw_data.get("pid") or "",
      "product_title": raw_data.get("product_title") or "",
      "brand": raw_data.get("brand") or "",
      "url": raw_data.get("url") or url,
      "availability": raw_data.get("availability") or "",
      "images": raw_data.get("images") or [],
      "mrp_inr": pricing.get("mrp_inr"),
      "current_selling_price_inr": pricing.get("current_selling_price_inr"),
      "selling_price_min_inr": price_range.get("min"),
      "selling_price_max_inr": price_range.get("max"),
      "discount_percentage": pricing.get("discount_percentage"),
      "primary_seller": seller_info.get("primary_seller") or "",
      "seller_rating": seller_info.get("seller_rating"),
      "processor": specifications.get("processor") or "",
      "ram": specifications.get("ram") or "",
      "storage": specifications.get("storage") or "",
      "operating_system": specifications.get("operating_system") or "",
      "display_size": specifications.get("display_size") or "",
      "resolution": specifications.get("resolution") or "",
      "color": design_and_build.get("color") or "",
      "weight_kg": design_and_build.get("weight_kg"),
      "software": extras.get("software") or "",
      "warranty": extras.get("warranty") or "",
  }


def process_flipkart_search_result(search_result: FlipkartSearchResult) -> bool:
  """Extract one candidate and upsert its PID marketplace record."""
  pid = (search_result.pid or "").strip()
  if not pid:
    raise ValueError("Flipkart search result is missing a PID.")
  product, _ = FlipkartProduct.objects.get_or_create(
      pid=pid,
      defaults={
          "search_result": search_result,
          "url": search_result.product_url,
          "status": ImportStatus.PENDING,
      },
  )

  product.status = ImportStatus.RUNNING
  product.error_message = ""
  product.url = search_result.product_url
  product.save(update_fields=["status", "error_message", "url", "updated_at"])

  try:
    data = extract_flipkart_product(search_result.product_url)
    with transaction.atomic():
      for field in (
          "product_title", "brand", "url", "availability", "images",
          "mrp_inr", "current_selling_price_inr", "selling_price_min_inr",
          "selling_price_max_inr", "discount_percentage", "primary_seller",
          "seller_rating", "processor", "ram", "storage", "operating_system",
          "display_size", "resolution", "color", "weight_kg", "software",
          "warranty",
      ):
        setattr(product, field, data.get(field))
      product.pid = pid
      product.status = ImportStatus.COMPLETED
      product.error_message = ""
      product.extracted_at = timezone.now()
      product.save()
      search_result.processed = True
      search_result.save(update_fields=["processed"])
  except Exception as exc:
    product.status = ImportStatus.FAILED
    product.error_message = str(exc) or exc.__class__.__name__
    product.save(update_fields=["status", "error_message", "updated_at"])
    raise

  return True


if __name__ == "__main__":
  target_url = input("Enter Flipkart Product URL: ").strip()

  print("\nExtracting data using Gemini API...")
  result_json = extract_flipkart_product_data(target_url)

  print(json.dumps(result_json, indent=2))
