from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import SearchKeyword
from .services.flipkart_search import (
    FlipkartSearchScrapingError,
    build_flipkart_search_url,
    search_flipkart_page,
)
from .services.flipkart_search_results import run_flipkart_search_for_keyword
from .services.scrapedo import ProviderResult, ScrapeDoError


HTML = """
<html><body>
  <a href="/apple/iphone/p/itm-one?pid=MOB00000001">Apple iPhone 16 128 GB</a>
  <a href="/apple/iphone/p/itm-two?pid=MOB00000002">Apple iPhone 16 256 GB</a>
  <a href="/apple/iphone/p/itm-one?pid=MOB00000001">Duplicate</a>
</body></html>
"""


@override_settings(SCRAPEDO_API_TOKEN="test-secret")
class ScrapeDoFlipkartSearchTests(TestCase):
    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_provider_html_is_parsed_and_target_url_is_encoded(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html=HTML, request_cost="3"
        )

        payload = search_flipkart_page("iphone 16", page=2)

        provider_cls.return_value.fetch.assert_called_once_with(
            "https://www.flipkart.com/search?q=iphone+16&page=2", render=True
        )
        self.assertEqual([item["pid"] for item in payload["results"]], ["MOB00000001", "MOB00000002"])
        self.assertEqual(payload["request_cost"], "3")

    def test_url_preserves_existing_search_semantics(self):
        self.assertEqual(
            build_flipkart_search_url("iphone 16"),
            "https://www.flipkart.com/search?q=iphone+16",
        )
        self.assertEqual(
            build_flipkart_search_url("iphone 16", 2),
            "https://www.flipkart.com/search?q=iphone+16&page=2",
        )

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_block_page_is_not_success(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html="<html>captcha robot check</html>"
        )
        with self.assertRaises(FlipkartSearchScrapingError):
            search_flipkart_page("iphone 16")

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_provider_error_propagates(self, provider_cls):
        provider_cls.return_value.fetch.side_effect = ScrapeDoError(
            "Scrape.do returned an HTTP error", status_code=429
        )
        with self.assertRaises(ScrapeDoError):
            search_flipkart_page("iphone 16")

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_token_is_not_logged(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html=HTML
        )
        with self.assertLogs("apps.importer.services.flipkart_search", level="INFO") as logs:
            search_flipkart_page("iphone 16")
        self.assertNotIn("test-secret", "\n".join(logs.output))

    @patch("apps.importer.services.flipkart_search_results.search_flipkart_page")
    def test_requested_pages_stop_at_last_available_page(self, search_page):
        page_result = {
            "results": [{
                "pid": "MOB00000001",
                "title": "Apple iPhone 16",
                "product_url": "https://www.flipkart.com/p/itm?pid=MOB00000001",
                "position": 1,
                "sponsored": False,
            }],
            "has_next": True,
            "request_cost": "1",
        }
        search_page.side_effect = [
            {**page_result, "has_next": page < 5}
            for page in range(1, 6)
        ]
        keyword = SearchKeyword.objects.create(keyword="iphone 16", amazon_pages=10)

        summary = run_flipkart_search_for_keyword(keyword)

        self.assertEqual(search_page.call_count, 5)
        self.assertEqual(summary.scraped_pages, 5)
        self.assertEqual(summary.available_pages, 5)
        self.assertEqual(summary.requested_pages, 10)
        self.assertIn("Requested 10 pages, but only 5 pages were available.", summary.reason)
        self.assertEqual(summary.successful, 1)
