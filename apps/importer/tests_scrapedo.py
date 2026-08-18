import logging
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .services.scrapedo import ScrapeDoAmazonProvider, ScrapeDoError, ScrapeDoWebProvider
from .services.scrapedo_health import check_scrapedo_health, validate_scrapedo_configuration


@override_settings(SCRAPEDO_API_TOKEN="test-token", SCRAPEDO_TIMEOUT=7, AMAZON_MARKETPLACE_GEOCODE="in")
class ScrapeDoProviderTests(SimpleTestCase):
    @patch("apps.importer.services.scrapedo.requests.get")
    def test_amazon_search_uses_india_geocode_and_maps_cost(self, get):
        response = Mock(ok=True, status_code=200)
        response.headers = {"Scrape.do-Request-Cost": "1"}
        response.json.return_value = {"status": "success", "products": [{"asin": "B000000001", "title": "Laptop", "url": "https://www.amazon.in/dp/B000000001", "position": 1}]}
        get.return_value = response
        result = ScrapeDoAmazonProvider().search("laptops", 2)
        self.assertTrue(result.success)
        self.assertEqual(result.request_cost, "1")
        self.assertEqual(get.call_args.kwargs["params"]["geocode"], "in")
        self.assertNotIn("test-token", result.as_dict().__repr__())

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_generic_provider_timeout_is_normalized(self, get):
        import requests
        get.side_effect = requests.Timeout()
        with self.assertRaisesRegex(Exception, "timed out"):
            ScrapeDoWebProvider().fetch("https://www.flipkart.com/search?q=laptops")

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_http_status_classification_and_duration(self, get):
        response = Mock(ok=False, status_code=429)
        response.headers = {"Scrape.do-Request-Cost": "3"}
        get.return_value = response
        with self.assertRaises(ScrapeDoError) as caught:
            ScrapeDoWebProvider().fetch("https://www.flipkart.com/search?q=laptops")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.request_cost, "3")
        self.assertIsNotNone(caught.exception.duration_ms)

        response.status_code = 400
        with self.assertRaises(ScrapeDoError) as caught:
            ScrapeDoWebProvider().fetch("https://www.flipkart.com/search?q=laptops")
        self.assertFalse(caught.exception.retryable)

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_request_cost_and_duration_are_returned(self, get):
        response = Mock(ok=True, status_code=200, text="<html>ok</html>")
        response.headers = {"Scrape.do-Request-Cost": "2.5"}
        get.return_value = response
        result = ScrapeDoWebProvider().fetch("https://example.com")
        self.assertEqual(result.request_cost, "2.5")
        self.assertGreaterEqual(result.duration_ms, 0)

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_amazon_500_is_retryable(self, get):
        response = Mock(ok=False, status_code=500)
        response.headers = {}
        get.return_value = response
        with self.assertRaises(ScrapeDoError) as caught:
            ScrapeDoAmazonProvider().search("laptops")
        self.assertTrue(caught.exception.retryable)

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_amazon_invalid_json_is_permanent(self, get):
        response = Mock(ok=True, status_code=200)
        response.headers = {}
        response.json.side_effect = ValueError("not json")
        get.return_value = response
        with self.assertRaises(ScrapeDoError) as caught:
            ScrapeDoAmazonProvider().search("laptops")
        self.assertFalse(caught.exception.retryable)

    @patch("apps.importer.services.scrapedo.requests.get")
    def test_token_is_not_written_to_logs(self, get,):
        response = Mock(ok=True, status_code=200, text="ok")
        response.headers = {}
        get.return_value = response
        with self.assertLogs("apps.importer.services.scrapedo", level=logging.INFO) as logs:
            ScrapeDoWebProvider().fetch("https://example.com")
        self.assertNotIn("test-token", "\n".join(logs.output))

    @patch("apps.importer.services.scrapedo_health.requests.head")
    def test_health_check_uses_lightweight_endpoint_checks(self, head):
        head.side_effect = [Mock(status_code=405), Mock(status_code=405)]
        result = check_scrapedo_health()
        self.assertTrue(result.healthy)
        self.assertEqual(head.call_count, 2)
        self.assertNotIn("test-token", repr(result.as_dict()))

    @override_settings(SCRAPEDO_API_TOKEN="")
    def test_configuration_validation_is_safe(self):
        self.assertEqual(validate_scrapedo_configuration(), {"token_configured": False, "timeout": 7})
        with self.assertRaisesRegex(RuntimeError, "SCRAPEDO_API_TOKEN"):
            validate_scrapedo_configuration(strict=True)
