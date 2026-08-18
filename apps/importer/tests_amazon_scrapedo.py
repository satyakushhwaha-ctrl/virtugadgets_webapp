from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .services.amazon_search import (
    DEFAULT_AMAZON_SORTING,
    normalize_scrapedo_amazon_search_result,
    search_amazon_page,
)
from .services.scrapedo import ProviderResult


@override_settings(SCRAPEDO_API_TOKEN="phase2-token", AMAZON_MARKETPLACE_GEOCODE="in")
class ScrapeDoAmazonSearchTests(SimpleTestCase):
    def _item(self, asin="B000000001"):
        return {
            "asin": asin,
            "title": "Example laptop",
            "url": f"https://www.amazon.in/dp/{asin}",
            "imageUrl": "https://m.media-amazon.com/images/I/example.jpg",
            "price": {"amount": 49999, "currencyCode": "INR"},
            "rating": {"value": 4.5, "count": 120},
            "reviewCount": "120",
            "isSponsored": False,
            "isPrime": True,
            "position": 1,
            "badge": "Best Seller",
        }

    @patch("apps.importer.services.scrapedo.ScrapeDoAmazonProvider.search")
    def test_featured_uses_structured_amazon_api(self, search):
        search.return_value = ProviderResult(
            True, "scrapedo_amazon", 200,
            data={"status": "success", "products": [self._item()], "totalResults": "32"},
            request_cost="3",
        )
        result = search_amazon_page("laptops", DEFAULT_AMAZON_SORTING, 2)
        search.assert_called_once_with("laptops", 2)
        self.assertEqual(result["source"], "scrapedo_amazon")
        self.assertEqual(result["request_cost"], "3")
        self.assertEqual(result["results"][0]["asin"], "B000000001")

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider.fetch")
    def test_non_featured_sort_preserves_exact_amazon_url(self, fetch):
        fetch.return_value = ProviderResult(True, "scrapedo_web", 200, html="<html></html>", request_cost="7")
        # Empty HTML is a valid no-more-pages response; the important contract
        # here is that the exact Amazon URL is handed to Scrape.do.
        result = search_amazon_page("laptops", "price-asc-rank", 3)
        target_url = fetch.call_args.args[0]
        self.assertIn("k=laptops", target_url)
        self.assertIn("s=price-asc-rank", target_url)
        self.assertIn("page=3", target_url)
        self.assertEqual(result["source"], "scrapedo_web")

    def test_normalizer_maps_structured_fields_without_schema_changes(self):
        result = normalize_scrapedo_amazon_search_result(self._item())
        self.assertEqual(result["image_url"], "https://m.media-amazon.com/images/I/example.jpg")
        self.assertEqual(result["price"], 49999)
        self.assertEqual(result["rating"], 4.5)
        self.assertEqual(result["review_count"], "120")
        self.assertEqual(result["badge"], "Best Seller")

    @patch("apps.importer.services.scrapedo.ScrapeDoAmazonProvider.search")
    def test_http_provider_error_is_not_marked_as_success(self, search):
        from .services.scrapedo import ScrapeDoError
        search.side_effect = ScrapeDoError("Scrape.do Amazon returned an HTTP error", status_code=503)
        with self.assertRaises(ScrapeDoError):
            search_amazon_page("laptops", DEFAULT_AMAZON_SORTING, 1)
