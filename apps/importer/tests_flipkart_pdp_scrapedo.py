from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import FlipkartProduct, FlipkartSearchResult, ImportStatus
from .services.flipkart_product import (
    extract_flipkart_product,
    extract_flipkart_product_data,
    process_flipkart_search_result,
)
from .services.scrapedo import ProviderResult, ScrapeDoError


PRODUCT_URL = "https://www.flipkart.com/apple-phone/p/itm-product?pid=MOBPDP0001"
PRODUCT_HTML = """
<html><body>
<script type="application/ld+json">
{
  "@context": "https://schema.org", "@type": "Product", "sku": "MOBPDP0001",
  "name": "Apple Example Phone", "brand": {"@type": "Brand", "name": "Apple"},
  "description": "A useful phone.",
  "image": ["https://rukminim2.flixcart.com/image/one.jpg", "https://rukminim2.flixcart.com/image/one.jpg", "https://rukminim2.flixcart.com/image/two.jpg", "https://example.com/placeholder.jpg"],
  "offers": {"price": "89999", "priceCurrency": "INR", "availability": "https://schema.org/InStock"},
  "aggregateRating": {"ratingValue": "4.5", "reviewCount": "120"},
  "additionalProperty": [{"name": "RAM", "value": "8 GB"}, {"name": "Storage", "value": "256 GB"}]
}
</script>
<div class="_3auQ3N">₹99,999</div>
<div id="sellerName">RetailNet</div>
<table><tr><th>Operating System</th><td>iOS</td></tr><tr><th>Color</th><td>Black</td></tr></table>
<div>Free Delivery by tomorrow</div>
</body></html>
"""


@override_settings(SCRAPEDO_API_TOKEN="flipkart-secret")
class ScrapeDoFlipkartProductTests(TestCase):
    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_exact_url_is_sent_and_html_is_parsed(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html=PRODUCT_HTML, request_cost="4"
        )

        raw = extract_flipkart_product_data(PRODUCT_URL)
        product = extract_flipkart_product(PRODUCT_URL)

        provider_cls.return_value.fetch.assert_called_with(PRODUCT_URL, render=True)
        self.assertEqual(raw["product"]["pid"], "MOBPDP0001")
        self.assertEqual(product["product_title"], "Apple Example Phone")
        self.assertEqual(product["brand"], "Apple")
        self.assertEqual(product["current_selling_price_inr"], 89999)
        self.assertEqual(product["mrp_inr"], 99999)
        self.assertEqual(product["primary_seller"], "RetailNet")
        self.assertEqual(product["availability"], "IN_STOCK")
        self.assertEqual(product["images"], [
            "https://rukminim2.flixcart.com/image/one.jpg",
            "https://rukminim2.flixcart.com/image/two.jpg",
        ])
        self.assertEqual(product["ram"], "8 GB")
        self.assertEqual(product["storage"], "256 GB")
        self.assertEqual(product["operating_system"], "iOS")

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_block_page_and_invalid_html_do_not_complete(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html="<html>captcha robot check</html>"
        )
        result = self._search_result()

        with self.assertRaises(ValueError):
            process_flipkart_search_result(result)
        self.assertEqual(FlipkartProduct.objects.get(pid=result.pid).status, ImportStatus.FAILED)

        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html="<html></html>"
        )
        with self.assertRaises(RuntimeError):
            process_flipkart_search_result(result)

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_provider_error_propagates_and_product_fails(self, provider_cls):
        provider_cls.return_value.fetch.side_effect = ScrapeDoError(
            "Scrape.do returned an HTTP error", status_code=403
        )
        result = self._search_result()

        with self.assertRaises(ScrapeDoError):
            process_flipkart_search_result(result)
        self.assertEqual(FlipkartProduct.objects.get(pid=result.pid).status, ImportStatus.FAILED)

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_successful_save_marks_search_result_processed(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html=PRODUCT_HTML, request_cost="9"
        )
        result = self._search_result()

        self.assertTrue(process_flipkart_search_result(result))
        product = FlipkartProduct.objects.get(pid=result.pid)
        result.refresh_from_db()
        self.assertEqual(product.status, ImportStatus.COMPLETED)
        self.assertTrue(result.processed)
        self.assertEqual(extract_flipkart_product_data.last_provider_metadata["request_cost"], "9")

    @patch("apps.importer.services.scrapedo.ScrapeDoWebProvider")
    def test_token_is_not_logged(self, provider_cls):
        provider_cls.return_value.fetch.return_value = ProviderResult(
            True, "scrapedo_web", 200, html=PRODUCT_HTML
        )
        with self.assertLogs("apps.importer.services.flipkart_product", level="INFO") as logs:
            extract_flipkart_product(PRODUCT_URL)
        self.assertNotIn("flipkart-secret", "\n".join(logs.output))

    def _search_result(self):
        return FlipkartSearchResult.objects.create(
            pid="MOBPDP0001",
            title="Apple Example Phone",
            product_url=PRODUCT_URL,
            position=1,
        )
