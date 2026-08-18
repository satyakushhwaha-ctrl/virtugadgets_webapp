from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import AmazonProduct, AmazonSearchResult, ImportStatus, SearchKeyword
from .services.amazon_product import (
    build_amazon_product_quality_report,
    extract_amazon_product,
    normalize_scrapedo_amazon_product,
    process_amazon_search_result,
)
from .services.scrapedo import ProviderResult, ScrapeDoError


@override_settings(SCRAPEDO_API_TOKEN="pdp-test-token", AMAZON_MARKETPLACE_GEOCODE="in")
class ScrapeDoAmazonPDPTests(TestCase):
    def setUp(self):
        self.url = "https://www.amazon.in/dp/B000000001"
        self.payload = {
            "asin": "B000000001",
            "name": "Example Laptop",
            "brand": "Visit the Samsung Store",
            "url": self.url,
            "price": 49999,
            "list_price": 69999,
            "currency": "INR",
            "thumbnail": "https://m.media-amazon.com/images/I/main.jpg",
            "images": [
                {"url": "https://m.media-amazon.com/images/I/main.jpg"},
                {"url": "https://m.media-amazon.com/images/I/gallery.jpg"},
                {"url": "https://m.media-amazon.com/images/I/gallery.jpg"},
                {"url": "https://example.com/placeholder.png"},
            ],
            "technical_details": {
                "Brand": "Samsung",
                "Model": "Book 4",
                "Processor": "Intel Core i7",
                "RAM": "16 GB",
                "Storage": "512 GB SSD",
            },
            "description": "A laptop description.",
            "highlights": ["Fast processor", "Long battery"],
            "seller": {"name": "Amazon Seller", "rating": 4.8},
            "availability": {"status": "In Stock"},
            "rating": 4.5,
            "total_ratings": 120,
            "shipping_info": ["FREE delivery"],
            "status": "success",
        }

    def provider_result(self, payload=None):
        return ProviderResult(
            True,
            "scrapedo_amazon",
            200,
            data=payload or self.payload,
            request_cost="4",
        )

    @patch("apps.importer.services.scrapedo.ScrapeDoAmazonProvider.product")
    def test_pdp_uses_asin_and_maps_structured_fields(self, product):
        product.return_value = self.provider_result()
        data = extract_amazon_product(self.url)
        product.assert_called_once_with("B000000001")
        self.assertEqual(data["product_title"], "Example Laptop")
        self.assertEqual(data["brand"], "Samsung")
        self.assertEqual(data["current_selling_price_inr"], 49999)
        self.assertEqual(data["mrp_inr"], 69999)
        self.assertEqual(data["primary_seller"], "Amazon Seller")
        self.assertEqual(data["availability"], "In Stock")
        self.assertEqual(data["rating"], 4.5)
        self.assertEqual(data["review_count"], 120)
        self.assertEqual(data["description"], "A laptop description.")
        self.assertEqual(data["highlights"], ["Fast processor", "Long battery"])

    def test_normalizer_deduplicates_and_filters_images_and_preserves_specs(self):
        data = normalize_scrapedo_amazon_product(self.payload, self.url)
        self.assertEqual(len(data["images"]), 2)
        self.assertNotIn("placeholder", " ".join(data["images"]))
        self.assertEqual(data["specifications"]["Processor"], "Intel Core i7")
        self.assertEqual(data["processor"], "Intel Core i7")
        self.assertEqual(data["model"], "Book 4")

    @patch("apps.importer.services.scrapedo.ScrapeDoAmazonProvider.product")
    def test_mrp_alone_passes_but_missing_both_prices_fails(self, product):
        mrp_only = dict(self.payload, price=None, list_price=69999)
        product.return_value = self.provider_result(mrp_only)
        data = extract_amazon_product(self.url)
        self.assertEqual(build_amazon_product_quality_report(data)["valid"], True)

        invalid = dict(self.payload, price=None, list_price=None)
        product.return_value = self.provider_result(invalid)
        with self.assertRaisesRegex(ValueError, "price_or_mrp"):
            extract_amazon_product(self.url)

    @patch("apps.importer.services.scrapedo.ScrapeDoAmazonProvider.product")
    def test_successful_save_completes_and_failure_does_not(self, product):
        keyword = SearchKeyword.objects.create(keyword="laptops")
        result = AmazonSearchResult.objects.create(
            keyword=keyword,
            asin="B000000001",
            title="Example Laptop",
            product_url=self.url,
            position=1,
        )
        product.return_value = self.provider_result()
        self.assertTrue(process_amazon_search_result(result))
        saved = AmazonProduct.objects.get(asin="B000000001")
        self.assertEqual(saved.status, ImportStatus.COMPLETED)

        invalid = dict(self.payload, price=None, list_price=None)
        product.return_value = self.provider_result(invalid)
        with self.assertRaises(ValueError):
            process_amazon_search_result(result)
        saved.refresh_from_db()
        self.assertEqual(saved.status, ImportStatus.FAILED)

    @patch("apps.importer.services.scrapedo.ScrapeDoAmazonProvider.product")
    def test_provider_error_propagates_without_playwright_or_scrapingbee(self, product):
        product.side_effect = ScrapeDoError("Scrape.do Amazon returned an HTTP error", status_code=503)
        with self.assertRaises(ScrapeDoError):
            extract_amazon_product(self.url)

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_provider_sends_asin_and_india_geocode(self, get):
        response = type("Response", (), {
            "ok": True,
            "status_code": 200,
            "headers": {"Scrape.do-Request-Cost": "2"},
            "json": lambda self: self_payload,
        })()
        self_payload = {"status": "success", **self.payload}
        get.return_value = response
        from .services.scrapedo import ScrapeDoAmazonProvider
        result = ScrapeDoAmazonProvider().product("B000000001")
        self.assertTrue(result.success)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["asin"], "B000000001")
        self.assertEqual(params["geocode"], "in")
