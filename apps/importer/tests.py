import os
import tempfile
from io import StringIO
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from .models import (
    AmazonProduct,
    AmazonSearchResult,
    BatchStatus,
    FlipkartProduct,
    FlipkartSearchResult,
    ImportBatch,
    ImportStatus,
    ProductMatch,
    SearchKeyword,
)
from apps.categories.models import Category
from apps.core.tasks import extract_best_matched_flipkart_product as best_match_task
from apps.products.models import Product, ProductPrice
from .admin import (
    AmazonProductAdmin,
    AmazonSearchResultAdmin,
    FlipkartSearchStatusFilter,
    FlipkartProductAdmin,
    FlipkartSearchResultAdmin,
    ProductMatchAdminForm,
    ProductMatchAdmin,
)
from .services.amazon_product import process_amazon_search_result
from .services.flipkart_search import (
    build_flipkart_search_query,
    build_flipkart_search_queries,
    search_flipkart,
)
from .services.flipkart_search_results import search_and_save_flipkart_candidates
from .services.flipkart_search_results import run_flipkart_search_for_keyword
from .services.amazon_search import search_amazon
from .services.amazon_search import (
    PRODUCT_SELECTORS,
    AmazonSearchScrapingError,
    _blocking_reason,
    _scrape_search_results,
)
from .services.flipkart_product import (
    extract_flipkart_product,
    process_flipkart_search_result,
)
from .services.product_matching import (
    first_valid_image_url,
    match_products,
    normalize_capacity,
    normalize_color,
    rank_flipkart_search_result,
    rank_flipkart_search_results,
)
from .services.product_publisher import (
    PublishValidationError,
    approve_amazon_product,
    approve_flipkart_product,
    assign_amazon_product_categories,
    assign_staged_product_categories,
    associate_flipkart_product,
    publish_amazon_product,
    publish_flipkart_product,
    publish_product_match,
    unpublish_amazon_product,
    unpublish_flipkart_product,
)
from .services.batch_runner import run_batch
from .services.product_matching import run_product_matching_for_batch
from .services.best_flipkart_match import extract_best_matched_flipkart_product


MOCK_RESULTS = [
    {
        "asin": "B000000001",
        "title": "Example phone",
        "product_url": "https://www.amazon.in/dp/B000000001",
        "position": 1,
        "sponsored": False,
    },
    {
        "asin": "B000000002",
        "title": "Sponsored phone",
        "product_url": "https://www.amazon.in/dp/B000000002",
        "position": 2,
        "sponsored": True,
    },
]

MOCK_PRODUCT = {
    "product_title": "Example phone",
    "brand": "Example",
    "url": "https://www.amazon.in/dp/B000000001",
    "availability": "In Stock",
    "images": ["https://images.example/phone.jpg"],
    "mrp_inr": 50000,
    "current_selling_price_inr": 45000,
    "selling_price_min_inr": 45000,
    "selling_price_max_inr": 50000,
    "discount_percentage": 10,
    "primary_seller": "Example Seller",
    "seller_rating": 4.5,
    "processor": "Example Processor",
    "ram": "8 GB",
    "storage": "128 GB",
    "operating_system": "Android",
    "display_size": "6.1 inches",
    "resolution": "1080p",
    "color": "Black",
    "weight_kg": 0.2,
    "software": "None",
    "warranty": "1 year",
}

MOCK_FLIPKART_PRODUCT = {
    "pid": "MOB123456789",
    "product_title": "Apple iPhone Air",
    "brand": "Apple",
    "url": "https://www.flipkart.com/apple-iphone-air/p/itm1?pid=MOB123456789",
    "availability": "In Stock",
    "images": ["https://rukminim2.flixcart.com/image.jpg"],
    "mrp_inr": 99999,
    "current_selling_price_inr": 89999,
    "selling_price_min_inr": 89999,
    "selling_price_max_inr": 99999,
    "discount_percentage": 10,
    "primary_seller": "RetailNet",
    "seller_rating": 4.5,
    "processor": "A19 Pro",
    "ram": "8 GB",
    "storage": "256 GB",
    "operating_system": "iOS",
    "display_size": "6.5 inches",
    "resolution": "1234 x 567",
    "color": "Light Gold",
    "weight_kg": 0.16,
    "software": "None",
    "warranty": "1 year",
}


class ImporterTests(TestCase):
    def make_search_result(self, asin="B000000001", position=1):
        keyword = SearchKeyword.objects.get_or_create(keyword="iphone 16")[0]
        return AmazonSearchResult.objects.create(
            keyword=keyword,
            asin=asin,
            title="Example phone",
            product_url=f"https://www.amazon.in/dp/{asin}",
            position=position,
        )

    def test_search_keyword_creation(self):
        search_keyword = SearchKeyword.objects.create(keyword="iphone 16")

        self.assertEqual(search_keyword.status, ImportStatus.PENDING)
        self.assertEqual(search_keyword.total_results, 0)

    def test_duplicate_keyword_handling(self):
        first, created = SearchKeyword.objects.get_or_create(keyword="iphone 16")
        second, created_again = SearchKeyword.objects.get_or_create(keyword="iphone 16")

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SearchKeyword.objects.filter(keyword="iphone 16").count(), 1)

    def test_amazon_search_result_uniqueness(self):
        keyword = SearchKeyword.objects.create(keyword="iphone 16")
        AmazonSearchResult.objects.create(
            keyword=keyword,
            asin="B000000001",
            title="Example phone",
            product_url="https://www.amazon.in/dp/B000000001",
            position=1,
        )

        with self.assertRaises(IntegrityError):
            AmazonSearchResult.objects.create(
                keyword=keyword,
                asin="B000000001",
                title="Duplicate phone",
                product_url="https://www.amazon.in/dp/B000000001",
                position=2,
            )

    def test_amazon_product_creation(self):
        product = AmazonProduct.objects.create(
            asin="B000000001",
            url="https://www.amazon.in/dp/B000000001",
            product_title="Example phone",
            images=[],
        )

        self.assertEqual(product.status, ImportStatus.PENDING)
        self.assertEqual(product.asin, "B000000001")

    def test_first_valid_image_url_uses_first_valid_amazon_image(self):
        product = AmazonProduct(images=["", "not-a-url", " https://images.example/second.jpg "])

        self.assertEqual(
            first_valid_image_url(product),
            "https://images.example/second.jpg",
        )

    def test_first_valid_image_url_handles_missing_images(self):
        self.assertEqual(first_valid_image_url(AmazonProduct(images=[])), "")
        self.assertEqual(first_valid_image_url(AmazonProduct(images=None)), "")

    def test_amazon_product_duplicate_asin_is_rejected(self):
        AmazonProduct.objects.create(
            asin="B000000001",
            url="https://www.amazon.in/dp/B000000001",
        )

        with self.assertRaises(IntegrityError):
            AmazonProduct.objects.create(
                asin="B000000001",
                url="https://www.amazon.in/dp/B000000001",
            )

    @patch("apps.importer.services.amazon_product.extract_amazon_product_data")
    def test_detail_service_normalizes_mocked_scraper_response(self, scraper):
        scraper.return_value = {
            "product_title": "Example phone",
            "brand": "Example",
            "url": "https://www.amazon.in/dp/B000000001",
            "availability": "In Stock",
            "images": ["https://images.example/phone.jpg"],
            "pricing": {
                "mrp_inr": 50000,
                "current_selling_price_inr": 45000,
                "selling_price_range_inr": {"min": 45000, "max": 50000},
                "discount_percentage": 10,
            },
            "seller_info": {"primary_seller": "Example Seller", "seller_rating": 4.5},
            "specifications": {"processor": "Example Processor"},
            "design_and_build": {"color": "Black", "weight_kg": 0.2},
            "extras": {"warranty": "1 year"},
        }

        from .services.amazon_product import extract_amazon_product

        product = extract_amazon_product("https://www.amazon.in/dp/B000000001")

        self.assertEqual(product["current_selling_price_inr"], 45000)
        self.assertEqual(product["selling_price_min_inr"], 45000)
        self.assertEqual(product["primary_seller"], "Example Seller")
        scraper.assert_called_once()

    @patch("apps.importer.services.amazon_product.extract_amazon_product_data")
    def test_new_amazon_scraper_shape_maps_nested_fields(self, scraper):
        scraper.return_value = {
            "url": "https://www.amazon.in/dp/B000000001",
            "product": {"asin": "B000000001", "title": "Nested phone", "brand": "Example"},
            "pricing": {"selling_price": 45000, "mrp": 50000, "min_price": 45000, "max_price": 45000, "discount_percentage": 10},
            "seller": {"name": "Buy Box Seller", "rating": 4.7},
            "availability": {"status": "IN_STOCK"},
            "images": ["https://images.example/first.jpg"],
            "specifications": {"Processor Name": "Example CPU", "Weight": "1340 Grams", "Colour": "Black"},
        }
        result = self.make_search_result()

        self.assertTrue(process_amazon_search_result(result))
        product = AmazonProduct.objects.get(asin=result.asin)
        self.assertEqual(product.current_selling_price_inr, 45000)
        self.assertEqual(product.mrp_inr, 50000)
        self.assertEqual(product.images[0], "https://images.example/first.jpg")
        self.assertEqual(product.processor, "Example CPU")
        self.assertEqual(float(product.weight_kg), 1.34)
        self.assertEqual(product.approval_status, "pending")

    @patch("apps.importer.services.amazon_search._scrape_search_results")
    def test_search_service_normalises_and_deduplicates_response(self, scraper):
        scraper.return_value = [
            {**MOCK_RESULTS[0], "product_url": "/dp/B000000001?tag=test"},
            MOCK_RESULTS[0],
            {**MOCK_RESULTS[1], "asin": ""},
        ]

        results = search_amazon(" iphone 16 ")

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["product_url"], "https://www.amazon.in/dp/B000000001")
        self.assertEqual(results[1]["asin"], "B000000002")
        scraper.assert_called_once_with("iphone 16")


    @patch("apps.importer.services.amazon_search_results.amazon_search.search_amazon")
    def test_management_command_saves_results(self, scraper):
        scraper.return_value = MOCK_RESULTS

        call_command("run_amazon_search", "iphone 16")

        keyword = SearchKeyword.objects.get(keyword="iphone 16")
        self.assertEqual(keyword.status, ImportStatus.COMPLETED)
        self.assertEqual(keyword.total_results, 2)
        self.assertEqual(keyword.amazon_results.count(), 2)

    @patch("apps.importer.services.amazon_search_results.amazon_search.search_amazon")
    def test_successful_scraper_status(self, scraper):
        scraper.return_value = MOCK_RESULTS
        keyword = SearchKeyword.objects.create(keyword="iphone 16")

        call_command("run_amazon_search", keyword.keyword)

        keyword.refresh_from_db()
        self.assertEqual(keyword.status, ImportStatus.COMPLETED)

    @patch("apps.importer.services.amazon_search_results.amazon_search.search_amazon")
    def test_failed_scraper_status(self, scraper):
        scraper.side_effect = TimeoutError("Amazon timed out")

        with self.assertRaises(CommandError):
            call_command("run_amazon_search", "iphone 16")

        keyword = SearchKeyword.objects.get(keyword="iphone 16")
        self.assertEqual(keyword.status, ImportStatus.FAILED)

    @patch("apps.importer.services.amazon_product.extract_amazon_product")
    def test_successful_product_extraction_marks_result_processed(self, extractor):
        extractor.return_value = MOCK_PRODUCT
        result = self.make_search_result()

        self.assertTrue(process_amazon_search_result(result))

        result.refresh_from_db()
        product = AmazonProduct.objects.get(asin=result.asin)
        self.assertTrue(result.processed)
        self.assertEqual(product.status, ImportStatus.COMPLETED)
        self.assertEqual(product.product_title, "Example phone")
        self.assertIsNotNone(product.extracted_at)

    @patch("apps.importer.services.amazon_product.extract_amazon_product")
    def test_failed_product_extraction_records_error(self, extractor):
        extractor.side_effect = TimeoutError("Amazon detail timed out")
        result = self.make_search_result()

        with self.assertRaises(TimeoutError):
            process_amazon_search_result(result)

        result.refresh_from_db()
        product = AmazonProduct.objects.get(asin=result.asin)
        self.assertFalse(result.processed)
        self.assertEqual(product.status, ImportStatus.FAILED)
        self.assertIn("Amazon detail timed out", product.error_message)

    @patch("apps.importer.services.amazon_product.extract_amazon_product")
    def test_completed_product_is_updated_on_reextraction(self, extractor):
        result = self.make_search_result()
        AmazonProduct.objects.create(
            asin=result.asin,
            url=result.product_url,
            status=ImportStatus.COMPLETED,
        )
        extractor.return_value = {**MOCK_PRODUCT, "product_title": "Updated phone"}

        self.assertTrue(process_amazon_search_result(result))

        extractor.assert_called_once()
        result.refresh_from_db()
        self.assertEqual(AmazonProduct.objects.get(asin=result.asin).product_title, "Updated phone")
        self.assertTrue(result.processed)

    @patch("apps.importer.services.amazon_product.extract_amazon_product")
    def test_failed_product_is_retried(self, extractor):
        result = self.make_search_result()
        extractor.side_effect = [RuntimeError("temporary failure"), MOCK_PRODUCT]

        with self.assertRaises(RuntimeError):
            process_amazon_search_result(result)
        self.assertTrue(process_amazon_search_result(result))

        product = AmazonProduct.objects.get(asin=result.asin)
        self.assertEqual(product.status, ImportStatus.COMPLETED)
        result.refresh_from_db()
        self.assertTrue(result.processed)
        self.assertEqual(extractor.call_count, 2)

    @patch("apps.importer.management.commands.extract_amazon_products.process_amazon_search_result")
    def test_extract_command_limit(self, process_result):
        keyword = SearchKeyword.objects.create(keyword="iphone 16")
        for index in range(3):
            AmazonSearchResult.objects.create(
                keyword=keyword,
                asin=f"B0000000{index + 1:02d}",
                title=f"Phone {index + 1}",
                product_url=f"https://www.amazon.in/dp/B0000000{index + 1:02d}",
                position=index + 1,
            )
        process_result.return_value = True

        call_command("extract_amazon_products", "iphone 16", limit=2)

        self.assertEqual(process_result.call_count, 2)
        self.assertEqual(
            {call.args[0].asin for call in process_result.call_args_list},
            {"B000000001", "B000000002"},
        )

    def make_completed_amazon_product(self, asin="B0FQFTV1NP", title="Apple iPhone 16"):
        return AmazonProduct.objects.create(
            asin=asin,
            product_title=title,
            brand="Apple",
            ram="8 GB",
            storage="128 GB",
            color="Black",
            url=f"https://www.amazon.in/dp/{asin}",
            status=ImportStatus.COMPLETED,
        )

    def test_flipkart_search_result_creation(self):
        product = self.make_completed_amazon_product()

        result = FlipkartSearchResult.objects.create(
            amazon_product=product,
            pid="MOB123456789",
            title="Apple iPhone 16",
            product_url="https://www.flipkart.com/apple-iphone/p/itm123?pid=MOB123456789",
            position=1,
        )

        self.assertEqual(result.amazon_product, product)
        self.assertFalse(result.processed)

    def test_flipkart_duplicate_pid_is_rejected(self):
        product = self.make_completed_amazon_product()
        values = {
            "amazon_product": product,
            "pid": "MOB123456789",
            "title": "Apple iPhone 16",
            "product_url": "https://www.flipkart.com/apple-iphone/p/itm123?pid=MOB123456789",
            "position": 1,
        }
        FlipkartSearchResult.objects.create(**values)

        with self.assertRaises(IntegrityError):
            FlipkartSearchResult.objects.create(**values)

    def test_flipkart_search_query_generation(self):
        product = self.make_completed_amazon_product(
            title="Apple iPhone 16 (Black, 128 GB) | Latest Phone"
        )

        query = build_flipkart_search_query(product)

        self.assertEqual(query, "Apple iPhone 16 8GB 128GB Black")
        self.assertNotIn("Latest Phone", query)

    def test_product_aware_laptop_query_prioritizes_model_and_core_specs(self):
        product = self.make_completed_amazon_product(
            title=(
                "HP Victus, AMD Ryzen 7 7445HS, 6GB RTX 4050, 16GB DDR5, "
                "512GB SSD, fb3130AX Gaming Laptop, Blue, Office24, Xbox GamePass"
            )
        )
        product.brand = "HP"
        product.processor = "AMD Ryzen 7 7445HS"
        product.ram = "16 GB"
        product.storage = "512 GB SSD"
        product.save()

        query = build_flipkart_search_query(product)

        for value in ("HP", "Victus", "fb3130AX", "Ryzen 7 7445HS", "RTX 4050", "16GB", "512GB"):
            self.assertIn(value, query)
        for noise in ("Xbox", "GamePass", "Office24", "Blue", "Upgradeable", "2.29kg"):
            self.assertNotIn(noise.casefold(), query.casefold())

    def test_product_aware_queries_cover_phone_tv_and_headphones(self):
        phone = self.make_completed_amazon_product(title="Apple iPhone 16 256GB")
        phone.brand = "Apple"
        phone.storage = "256GB"
        phone.save()
        self.assertEqual(build_flipkart_search_query(phone), "Apple iPhone 16 8GB 256GB Black")

        tv = self.make_completed_amazon_product(asin="B0FQFTV1NQ", title="Samsung 55 inch 4K QLED Smart TV")
        tv.brand = "Samsung"
        tv.save()
        tv_query = build_flipkart_search_query(tv)
        self.assertIn("Samsung", tv_query)
        self.assertIn("55", tv_query)
        self.assertIn("4K", tv_query)
        self.assertIn("QLED", tv_query)

        headphones = self.make_completed_amazon_product(asin="B0FQFTV1NR", title="Sony WH-1000XM5 Wireless Headphones")
        headphones.brand = "Sony"
        headphones.save()
        self.assertIn("Sony", build_flipkart_search_query(headphones))
        self.assertIn("WH-1000XM5", build_flipkart_search_query(headphones))

    def test_product_aware_query_uses_available_fields_without_empty_values(self):
        product = self.make_completed_amazon_product(title="Lenovo ThinkPad 21T9005VIG")
        product.brand = "Lenovo"
        product.processor = ""
        product.ram = ""
        product.storage = ""
        product.save()

        queries = build_flipkart_search_queries(product)

        self.assertTrue(queries)
        self.assertIn("Lenovo", queries[0])
        self.assertIn("21T9005VIG", queries[0])
        self.assertNotIn("None", queries[0])
        self.assertNotIn("  ", queries[0])

    def test_weight_is_never_used_as_storage_or_ram(self):
        product = self.make_completed_amazon_product(
            title="Apple iPhone Air 256GB: Thinnest iPhone Ever, 16.63 cm Display"
        )
        product.storage = "256GB"
        product.ram = "0.1 GB"
        product.weight_kg = 0.1
        product.color = "Light Gold"
        product.save()

        query = build_flipkart_search_query(product)

        self.assertEqual(query, "Apple iPhone Air 256GB Light Gold")
        self.assertNotIn("0.1", query)
        self.assertNotIn("weight", query.casefold())

    def test_capacity_values_are_normalized(self):
        product = self.make_completed_amazon_product(title="Apple iPhone 16")
        product.storage = "256 GB ROM"
        product.ram = "8GB RAM"
        product.save()

        query = build_flipkart_search_query(product)

        self.assertIn("256GB", query)
        self.assertIn("8GB", query)
        self.assertNotIn("ROM", query)
        self.assertNotIn("RAM", query)

    def test_query_fallbacks_are_progressively_broader(self):
        product = self.make_completed_amazon_product(
            title="Apple iPhone Air 256GB: Promotion and camera details"
        )
        product.storage = "256GB"
        product.ram = ""
        product.color = "Light Gold"
        product.save()

        queries = build_flipkart_search_queries(product)

        self.assertEqual(queries, [
            "Apple iPhone Air 256GB Light Gold",
            "Apple iPhone Air",
            "Apple iPhone Air 256GB",
            "Apple",
        ])

    @patch("apps.importer.services.flipkart_search._scrape_search_results")
    def test_mocked_flipkart_search_normalizes_and_deduplicates(self, scraper):
        scraper.return_value = [
            {
                "title": "Apple iPhone 16",
                "product_url": "https://www.flipkart.com/apple/p/itm1?pid=MOB123456789",
                "position": 1,
                "sponsored": False,
            },
            {
                "title": "Duplicate Apple iPhone 16",
                "product_url": "https://www.flipkart.com/apple/p/itm2?pid=MOB123456789",
                "position": 2,
                "sponsored": True,
            },
            {
                "title": "Missing PID",
                "product_url": "https://www.flipkart.com/apple/p/itm3",
                "position": 3,
                "sponsored": False,
            },
        ]

        candidates = search_flipkart("Apple iPhone 16")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["pid"], "MOB123456789")
        scraper.assert_called_once_with("Apple iPhone 16")

    @patch("apps.importer.services.flipkart_search_results.search_flipkart")
    def test_flipkart_search_persists_candidates(self, search):
        product = self.make_completed_amazon_product()
        search.return_value = [
            {
                "pid": "MOB123456789",
                "title": "Apple iPhone 16",
                "product_url": "https://www.flipkart.com/apple/p/itm1?pid=MOB123456789",
                "position": 1,
                "sponsored": False,
            }
        ]

        summary = search_and_save_flipkart_candidates(product)

        self.assertEqual(summary.saved, 1)
        self.assertEqual(summary.candidates_selected, 1)
        self.assertEqual(product.flipkart_results.count(), 1)

    @patch("apps.importer.services.flipkart_search_results.search_flipkart")
    def test_best_match_search_can_persist_all_returned_candidates(self, search):
        product = self.make_completed_amazon_product()
        search.return_value = [
            {
                "pid": f"MOBALL{index:05d}",
                "title": f"Apple iPhone candidate {index}",
                "product_url": f"https://www.flipkart.com/phone/p/x?pid=MOBALL{index:05d}",
                "position": index,
                "sponsored": False,
            }
            for index in range(1, 23)
        ]

        summary = search_and_save_flipkart_candidates(product, candidate_limit=None)

        self.assertEqual(summary.candidates_found, 22)
        self.assertEqual(summary.candidates_selected, 22)
        self.assertEqual(len(summary.candidate_pids), 22)
        self.assertEqual(product.flipkart_results.count(), 22)

    @patch("apps.importer.management.commands.search_flipkart.search_and_save_flipkart_candidates")
    def test_search_flipkart_command_limit(self, search):
        keyword = SearchKeyword.objects.create(keyword="iphone 16")
        products = []
        for index in range(3):
            asin = f"B0FQFTV1N{index + 1:02d}"
            product = self.make_completed_amazon_product(asin=asin, title=f"Phone {index + 1}")
            products.append(product)
            AmazonSearchResult.objects.create(
                keyword=keyword,
                asin=asin,
                title=product.product_title,
                product_url=product.url,
                position=index + 1,
            )
        search.return_value = type("Summary", (), {
            "query": "Phone",
            "candidates_found": 1,
            "candidates_selected": 1,
            "saved": 1,
            "skipped_duplicates": 0,
        })()

        call_command("search_flipkart", "iphone 16", limit=2)

        self.assertEqual(search.call_count, 2)
        self.assertEqual(
            {call.args[0].asin for call in search.call_args_list},
            {product.asin for product in products[:2]},
        )

    def test_search_flipkart_missing_amazon_product(self):
        with self.assertRaises(CommandError):
            call_command("search_flipkart", asin="B0FQFTV1NP")

    @patch("apps.importer.services.flipkart_search_results.search_flipkart")
    def test_flipkart_search_failure_is_not_hidden(self, search):
        product = self.make_completed_amazon_product()
        search.side_effect = TimeoutError("Flipkart timed out")

        with self.assertRaises(TimeoutError):
            search_and_save_flipkart_candidates(product)

    @patch("apps.importer.services.flipkart_search_results.search_flipkart")
    def test_zero_results_trigger_progressive_search(self, search):
        product = self.make_completed_amazon_product(
            title="Apple iPhone Air 256GB: Marketing text"
        )
        product.storage = "256GB"
        product.ram = ""
        product.color = "Light Gold"
        product.save()
        candidate = {
            "pid": "MOB123456789",
            "title": "Apple iPhone Air",
            "product_url": "https://www.flipkart.com/apple/p/itm1?pid=MOB123456789",
            "position": 1,
            "sponsored": False,
        }
        search.side_effect = [[], [candidate]]

        summary = search_and_save_flipkart_candidates(product)

        self.assertEqual(search.call_count, 2)
        self.assertEqual(summary.query, "Apple iPhone Air")
        self.assertEqual(
            [(attempt.query, attempt.candidates_found) for attempt in summary.attempts],
            [
                ("Apple iPhone Air 256GB Light Gold", 0),
                ("Apple iPhone Air", 1),
            ],
        )

    @patch("apps.importer.services.flipkart_search_results.search_flipkart")
    def test_run_flipkart_search_for_keyword_tracks_progress_with_batch(self, search):
        keyword = SearchKeyword.objects.create(keyword="iphone 16")
        search.return_value = [
            {
                "pid": "MOBTRACK0001",
                "title": "Apple iPhone 16 256 GB Black",
                "product_url": "https://www.flipkart.com/apple/p/itm?pid=MOBTRACK0001",
                "position": 1,
                "sponsored": False,
            },
            {
                "pid": "MOBTRACK0002",
                "title": "Apple iPhone 16 Plus 256 GB Blue",
                "product_url": "https://www.flipkart.com/apple/p/itm?pid=MOBTRACK0002",
                "position": 2,
                "sponsored": False,
            },
        ]

        summary = run_flipkart_search_for_keyword(keyword)

        batch = ImportBatch.objects.latest("created_at")
        self.assertEqual(summary.amazon_products_total, 1)
        self.assertEqual(summary.successful, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(batch.status, BatchStatus.COMPLETED)
        self.assertEqual(batch.amazon_products_count, 1)
        self.assertEqual(batch.successful_count, 1)
        self.assertEqual(batch.failed_count, 0)
        self.assertEqual(batch.flipkart_results_count, 2)
        self.assertEqual(keyword.import_batches.count(), 1)
        self.assertEqual(
            FlipkartSearchResult.objects.filter(
                batches=batch,
            ).count(),
            2,
        )

    @patch("apps.importer.services.flipkart_search_results.search_flipkart")
    def test_run_flipkart_search_for_keyword_marks_batch_failed(self, search):
        keyword = SearchKeyword.objects.create(keyword="iphone 16 pro")
        search.side_effect = TimeoutError("Flipkart timed out")

        summary = run_flipkart_search_for_keyword(keyword)

        batch = ImportBatch.objects.latest("created_at")
        self.assertEqual(summary.successful, 0)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(batch.status, BatchStatus.FAILED)
        self.assertEqual(batch.successful_count, 0)
        self.assertEqual(batch.failed_count, 1)
        self.assertIn("Flipkart search failed", batch.error_message)

    def make_flipkart_search_result(
        self,
        pid="MOB123456789",
        position=1,
        asin="B0FQFTV1NP",
    ):
        product = self.make_completed_amazon_product(asin=asin)
        return FlipkartSearchResult.objects.create(
            amazon_product=product,
            pid=pid,
            title="Apple iPhone Air",
            product_url=(
                "https://www.flipkart.com/apple-iphone-air/p/itm1?pid="
                f"{pid}"
            ),
            position=position,
        )

    def test_flipkart_product_creation(self):
        result = self.make_flipkart_search_result()

        product = FlipkartProduct.objects.create(
            search_result=result,
            pid=result.pid,
            url=result.product_url,
            product_title="Apple iPhone Air",
            images=[],
        )

        self.assertEqual(product.status, ImportStatus.PENDING)
        self.assertEqual(product.search_result, result)

    def test_flipkart_product_duplicate_pid_is_rejected(self):
        first = self.make_flipkart_search_result()
        second = self.make_flipkart_search_result(
            pid="MOB987654321",
            asin="B0FQFTV1NQ",
        )

        FlipkartProduct.objects.create(
            search_result=first,
            pid="MOB123456789",
            url=first.product_url,
        )
        with self.assertRaises(IntegrityError):
            FlipkartProduct.objects.create(
                search_result=second,
                pid="MOB123456789",
                url=second.product_url,
            )

    @patch("apps.importer.services.flipkart_product.extract_flipkart_product")
    def test_successful_flipkart_extraction_marks_result_processed(self, extractor):
        extractor.return_value = MOCK_FLIPKART_PRODUCT
        result = self.make_flipkart_search_result()

        self.assertTrue(process_flipkart_search_result(result))

        result.refresh_from_db()
        product = FlipkartProduct.objects.get(search_result=result)
        self.assertTrue(result.processed)
        self.assertEqual(product.status, ImportStatus.COMPLETED)
        self.assertEqual(product.product_title, "Apple iPhone Air")
        self.assertIsNotNone(product.extracted_at)

    @patch("apps.importer.services.flipkart_product.extract_flipkart_product_data")
    def test_new_flipkart_scraper_shape_maps_nested_fields(self, scraper):
        scraper.return_value = {
            "url": "https://www.flipkart.com/phone/p/itm1?pid=MOB123456789",
            "product": {"pid": "MOB123456789", "sku": "MOB123456789", "title": "Nested phone", "brand": "Example"},
            "pricing": {"selling_price": 89999, "mrp": 99999, "min_price": 89999, "max_price": 89999, "discount_percentage": 10},
            "seller": {"name": "RetailNet", "id": "SELLER1"},
            "availability": {"status": "IN_STOCK"},
            "images": ["https://rukminim2.flixcart.com/first.jpg"],
            "specifications": {"RAM": "8 GB", "Storage": "256 GB"},
        }
        result = self.make_flipkart_search_result()

        self.assertTrue(process_flipkart_search_result(result))
        product = FlipkartProduct.objects.get(pid=result.pid)
        self.assertEqual(product.current_selling_price_inr, 89999)
        self.assertEqual(product.mrp_inr, 99999)
        self.assertEqual(product.images[0], "https://rukminim2.flixcart.com/first.jpg")
        self.assertEqual(product.ram, "8 GB")
        self.assertEqual(product.approval_status, "pending")

    @patch("apps.importer.services.flipkart_product.extract_flipkart_product")
    def test_failed_flipkart_extraction_is_retryable(self, extractor):
        extractor.side_effect = [TimeoutError("Flipkart detail timed out"), MOCK_FLIPKART_PRODUCT]
        result = self.make_flipkart_search_result()

        with self.assertRaises(TimeoutError):
            process_flipkart_search_result(result)
        result.refresh_from_db()
        product = FlipkartProduct.objects.get(search_result=result)
        self.assertEqual(product.status, ImportStatus.FAILED)
        self.assertFalse(result.processed)
        self.assertIn("Flipkart detail timed out", product.error_message)

        self.assertTrue(process_flipkart_search_result(result))
        product.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(product.status, ImportStatus.COMPLETED)
        self.assertTrue(result.processed)

    @patch("apps.importer.services.flipkart_product.extract_flipkart_product")
    def test_completed_flipkart_product_is_updated_on_reextraction(self, extractor):
        result = self.make_flipkart_search_result()
        FlipkartProduct.objects.create(
            search_result=result,
            pid=result.pid,
            url=result.product_url,
            status=ImportStatus.COMPLETED,
        )
        extractor.return_value = {**MOCK_FLIPKART_PRODUCT, "product_title": "Updated Flipkart phone"}

        self.assertTrue(process_flipkart_search_result(result))

        extractor.assert_called_once()
        result.refresh_from_db()
        self.assertEqual(FlipkartProduct.objects.get(pid=result.pid).product_title, "Updated Flipkart phone")
        self.assertTrue(result.processed)

    @patch("apps.importer.services.flipkart_product.extract_flipkart_product")
    def test_processed_flag_changes_only_after_success(self, extractor):
        result = self.make_flipkart_search_result()
        extractor.side_effect = RuntimeError("temporary failure")

        with self.assertRaises(RuntimeError):
            process_flipkart_search_result(result)

        result.refresh_from_db()
        self.assertFalse(result.processed)

    @patch("apps.importer.management.commands.extract_flipkart_products.process_flipkart_search_result")
    def test_extract_flipkart_products_limit(self, process_result):
        source = self.make_completed_amazon_product()
        results = []
        for index in range(3):
            result = FlipkartSearchResult.objects.create(
                amazon_product=source,
                pid=f"MOB12345678{index}",
                title=f"Phone {index}",
                product_url=(
                    "https://www.flipkart.com/phone/p/itm1?pid="
                    f"MOB12345678{index}"
                ),
                position=index + 1,
            )
            results.append(result)
        process_result.return_value = True

        call_command(
            "extract_flipkart_products",
            asin=source.asin,
            limit=2,
        )

        self.assertEqual(process_result.call_count, 2)
        self.assertEqual(
            {call.args[0].pid for call in process_result.call_args_list},
            {results[0].pid, results[1].pid},
        )

    def test_invalid_flipkart_product_url_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_flipkart_product("")

    def make_matching_pair(
        self,
        flipkart_title="Apple iPhone Air (Light Gold, 256 GB)",
        flipkart_pid="MOBMATCH123456",
        flipkart_overrides=None,
        amazon_asin="B0MATCH00001",
    ):
        amazon = AmazonProduct.objects.create(
            asin=amazon_asin,
            product_title="Apple iPhone Air 256 GB Light Gold",
            brand="Apple",
            storage="256 GB",
            ram="8GB RAM",
            color="Light Gold",
            processor="A19 Pro",
            display_size="6.5 inches",
            resolution="1234 x 567",
            url="https://www.amazon.in/dp/B0MATCH00001",
            status=ImportStatus.COMPLETED,
        )
        search_result = FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid=flipkart_pid,
            title=flipkart_title,
            product_url=(
                "https://www.flipkart.com/apple-iphone-air/p/itm1?pid="
                f"{flipkart_pid}"
            ),
            position=1,
            processed=True,
        )
        values = {
            "search_result": search_result,
            "pid": flipkart_pid,
            "product_title": flipkart_title,
            "brand": "Apple",
            "storage": "256GB ROM",
            "ram": "8 GB",
            "color": "light gold",
            "processor": "A19 Pro",
            "display_size": "6.5 inches",
            "resolution": "1234 x 567",
            "url": search_result.product_url,
            "status": ImportStatus.COMPLETED,
        }
        values.update(flipkart_overrides or {})
        flipkart = FlipkartProduct.objects.create(**values)
        return amazon, flipkart

    def test_same_structured_product_is_high_match(self):
        amazon, flipkart = self.make_matching_pair()

        result = match_products(amazon, flipkart)

        self.assertGreaterEqual(result["score"], 85)
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["match_status"], "matched")

    def test_different_brand_is_rejected(self):
        amazon, flipkart = self.make_matching_pair(
            flipkart_overrides={"brand": "Samsung"}
        )

        result = match_products(amazon, flipkart)

        self.assertEqual(result["match_status"], "rejected")
        self.assertEqual(result["confidence"], "low")
        self.assertTrue(result["reasons"]["brand"]["hard_mismatch"])

    def test_different_model_is_rejected(self):
        amazon, flipkart = self.make_matching_pair(
            flipkart_title="Apple iPhone 16 Pro 256 GB"
        )

        result = match_products(amazon, flipkart)

        self.assertEqual(result["match_status"], "rejected")
        self.assertTrue(result["reasons"]["model"]["hard_mismatch"])

    def test_different_storage_is_rejected(self):
        amazon, flipkart = self.make_matching_pair(
            flipkart_overrides={"storage": "512 GB"}
        )

        result = match_products(amazon, flipkart)

        self.assertEqual(result["match_status"], "rejected")
        self.assertTrue(result["reasons"]["storage"]["hard_mismatch"])

    def test_capacity_and_color_normalization(self):
        self.assertEqual(normalize_capacity("256GB"), "256 gb")
        self.assertEqual(normalize_capacity("256 GB"), "256 gb")
        self.assertEqual(normalize_capacity("256 gb"), "256 gb")
        self.assertEqual(normalize_color("Light Gold"), "light gold")
        self.assertEqual(normalize_color("light-gold"), "light gold")

    def test_missing_color_does_not_reject(self):
        amazon, flipkart = self.make_matching_pair(
            flipkart_overrides={"color": ""}
        )

        result = match_products(amazon, flipkart)

        self.assertNotEqual(result["match_status"], "rejected")
        self.assertIsNone(result["reasons"]["color"]["matched"])

    def test_different_explicit_colors_reduce_confidence(self):
        amazon, flipkart = self.make_matching_pair(
            flipkart_overrides={"color": "Space Black"}
        )

        result = match_products(amazon, flipkart)

        self.assertEqual(result["confidence"], "medium")
        self.assertEqual(result["match_status"], "review")

    def test_different_ram_is_rejected(self):
        amazon, flipkart = self.make_matching_pair(
            flipkart_overrides={"ram": "16 GB"}
        )

        result = match_products(amazon, flipkart)

        self.assertEqual(result["match_status"], "rejected")
        self.assertTrue(result["reasons"]["ram"]["hard_mismatch"])

    def test_phone_accessories_are_hard_rejected(self):
        amazon, _ = self.make_matching_pair(
            flipkart_title="Apple iPhone Air 256 GB Light Gold",
        )
        for accessory_title in (
            "Phone Case for Apple iPhone Air",
            "Screen Guard for Apple iPhone Air",
            "Back Camera Lens Glass Protector for Apple iPhone Air",
            "Charger for Apple iPhone Air",
        ):
            with self.subTest(accessory_title=accessory_title):
                search_result = FlipkartSearchResult.objects.create(
                    amazon_product=amazon,
                    pid=f"MOBACCESS{abs(hash(accessory_title)) % 1000000}",
                    title=accessory_title,
                    product_url="https://www.flipkart.com/accessory/p/itm1",
                    position=2,
                )
                accessory = FlipkartProduct.objects.create(
                    search_result=search_result,
                    pid=search_result.pid,
                    product_title=accessory_title,
                    brand="Apple",
                    url=search_result.product_url,
                    status=ImportStatus.COMPLETED,
                )
                result = match_products(amazon, accessory)
                self.assertEqual(result["match_status"], "rejected")
                self.assertTrue(result["reasons"]["product_type"]["hard_mismatch"])

    def test_matching_service_does_not_write_database(self):
        amazon, flipkart = self.make_matching_pair()
        before = ProductMatch.objects.count()

        match_products(amazon, flipkart)

        self.assertEqual(ProductMatch.objects.count(), before)

    def test_product_match_duplicate_pair_is_rejected(self):
        amazon, flipkart = self.make_matching_pair()
        values = {
            "amazon_product": amazon,
            "flipkart_product": flipkart,
            "score": 100,
            "confidence": "high",
            "match_status": "matched",
            "reasons": {},
        }
        ProductMatch.objects.create(**values)

        with self.assertRaises(IntegrityError):
            ProductMatch.objects.create(**values)

    def test_match_score_is_bounded(self):
        amazon, flipkart = self.make_matching_pair()
        result = match_products(amazon, flipkart)

        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_match_command_persists_all_and_prints_best_candidate(self):
        amazon, best = self.make_matching_pair()
        _, weaker = self.make_matching_pair(
            flipkart_title="Apple iPhone Air 512 GB",
            flipkart_pid="MOBMATCH654321",
            flipkart_overrides={"storage": "512 GB"},
            amazon_asin="B0MATCH00002",
        )
        weaker.search_result.amazon_product = amazon
        weaker.search_result.save(update_fields=["amazon_product"])

        call_command("match_products", asin=amazon.asin, limit=10)

        self.assertEqual(
            ProductMatch.objects.filter(amazon_product=amazon).count(),
            2,
        )
        best_match = ProductMatch.objects.get(flipkart_product=best)
        self.assertEqual(best_match.match_status, "matched")


class AmazonSearchBrowserDiagnosticsTests(SimpleTestCase):
    def _page(self, body_text, *, products=False, selector_timeout=False):
        page = MagicMock()
        page.url = "https://www.amazon.in/s?k=iphone+16"
        page.title.return_value = "Amazon.in Search"
        page.content.return_value = f"<html><body>{body_text}</body></html>"
        body = MagicMock()
        body.inner_text.return_value = body_text
        product_items = MagicMock()
        product_items.count.return_value = 1
        item = MagicMock()
        item.get_attribute.return_value = "B000000001"
        item.inner_text.return_value = "Example phone"
        link = MagicMock()
        link.count.return_value = 1
        link.first.get_attribute.return_value = "/dp/B000000001"
        title = MagicMock()
        title.count.return_value = 1
        title.first.inner_text.return_value = "Example phone"
        item.locator.side_effect = lambda selector: link if selector.startswith("h2 a") else title
        product_items.nth.return_value = item

        def locator(selector):
            if selector == "body":
                return body
            if products and selector in PRODUCT_SELECTORS:
                return product_items
            empty = MagicMock()
            empty.count.return_value = 0
            return empty

        page.locator.side_effect = locator
        if selector_timeout:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            page.wait_for_selector.side_effect = PlaywrightTimeoutError("selector timeout")
        return page

    def _runtime(self, page):
        runtime = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        runtime.chromium.launch.return_value = browser
        manager = MagicMock()
        manager.__enter__.return_value = runtime
        manager.__exit__.return_value = False
        return manager, browser, context

    @patch("playwright.sync_api.sync_playwright")
    def test_normal_search_results_use_fallback_aware_scraper(self, sync):
        page = self._page("Example phone", products=True)
        manager, browser, context = self._runtime(page)
        sync.return_value = manager

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PLAYWRIGHT_DIAGNOSTICS_DIR": directory}):
                results = _scrape_search_results("iphone 16")

        self.assertEqual(results[0]["asin"], "B000000001")
        self.assertEqual(results[0]["title"], "Example phone")
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_captcha_page_is_classified_as_blocked(self):
        diagnostics = {
            "url": "https://www.amazon.in/errors/validateCaptcha",
            "title": "Robot Check",
            "body_text": "Enter the characters you see below",
            "html_snippet": "captcha",
        }
        self.assertEqual(_blocking_reason(diagnostics), "captcha")

    def test_empty_page_raises_diagnostic_exception_with_artifacts(self):
        page = self._page("", products=False)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PLAYWRIGHT_DIAGNOSTICS_DIR": directory}):
                with self.assertRaises(AmazonSearchScrapingError) as raised:
                    from .services.amazon_search import _raise_unexpected_page
                    _raise_unexpected_page(page, "iphone 16", {
                        "url": page.url,
                        "title": page.title(),
                        "body_text": "",
                        "html_snippet": "<body></body>",
                        "html": "<html><body></body></html>",
                    })
        self.assertIn("product selectors were not found", str(raised.exception))
        self.assertIn("page.png", str(raised.exception))
        self.assertIn("page.html", str(raised.exception))

    @patch("playwright.sync_api.sync_playwright")
    def test_selector_timeout_raises_meaningful_diagnostic_exception(self, sync):
        page = self._page("Amazon search", selector_timeout=True)
        manager, browser, context = self._runtime(page)
        sync.return_value = manager

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PLAYWRIGHT_DIAGNOSTICS_DIR": directory}):
                with self.assertRaises(AmazonSearchScrapingError) as raised:
                    _scrape_search_results("iphone 16")

        message = str(raised.exception)
        self.assertIn("URL: https://www.amazon.in/s?k=iphone+16", message)
        self.assertIn("title: Amazon.in Search", message)
        context.close.assert_called_once()
        browser.close.assert_called_once()


class ImporterAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_importer_models_are_registered_in_admin(self):
        for model in (
            AmazonProduct,
            FlipkartSearchResult,
            FlipkartProduct,
            ProductMatch,
        ):
            self.assertIn(model, admin.site._registry)

    def test_amazon_search_result_admin_extraction_action_exists(self):
        model_admin = AmazonSearchResultAdmin(AmazonSearchResult, admin.site)

        self.assertIn("extract_amazon_products", model_admin.actions)

    def make_best_match_product(self, asin="B0BEST000001"):
        return AmazonProduct.objects.create(
            asin=asin,
            product_title="Apple iPhone 16 128 GB",
            brand="Apple",
            storage="128 GB",
            url=f"https://www.amazon.in/dp/{asin}",
            status=ImportStatus.COMPLETED,
        )

    def make_best_match_candidate(self, amazon, pid, title, position):
        return FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid=pid,
            title=title,
            product_url=f"https://www.flipkart.com/phone/p/x?pid={pid}",
            position=position,
        )

    @patch("apps.importer.services.best_flipkart_match.search_and_save_flipkart_candidates")
    @patch("apps.importer.services.flipkart_product.extract_flipkart_product")
    def test_best_match_extracts_only_highest_ranked_candidate(self, extractor, search):
        amazon = self.make_best_match_product()
        best = self.make_best_match_candidate(
            amazon, "BEST123", "Apple iPhone 16 128 GB", 2
        )
        lower = self.make_best_match_candidate(
            amazon, "LOW123", "Apple iPhone 16 512 GB", 1
        )
        search.return_value = SimpleNamespace(
            candidate_pids=(best.pid, lower.pid),
        )
        extractor.return_value = {**MOCK_FLIPKART_PRODUCT, "pid": best.pid}

        result = extract_best_matched_flipkart_product(amazon)

        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["match"]["candidate"].pk, best.pk)
        extractor.assert_called_once_with(best.product_url)
        self.assertFalse(FlipkartSearchResult.objects.get(pk=lower.pk).processed)
        self.assertTrue(FlipkartProduct.objects.filter(pid=best.pid).exists())
        self.assertEqual(AmazonProduct.objects.count(), 1)
        self.assertEqual(ProductMatch.objects.count(), 1)

    def test_title_matching_prioritizes_laptop_identity_signals(self):
        amazon = SimpleNamespace(
            product_title=(
                "HP Victus, AMD Ryzen 7 7445HS, 6GB RTX 4050, "
                "16GB DDR5(Upgradeable) 512GB SSD, 144Hz, IPS, 300 nits, "
                "15.6''/39.6cm, Win11, Office24, Blue, 2.29kg, fb3130AX, "
                "DTS Audio, Xbox Gamepass*, Gaming Laptop"
            ),
            brand="HP",
            processor="AMD Ryzen 7 7445HS",
            ram="16 GB",
            storage="512 GB",
            operating_system="Windows 11",
        )
        candidates = [
            "HP Victus AMD Ryzen 7 Hexa Core 7445HS (16 GB/512 GB SSD/Windows 11 Home/6 GB Graphics/NVIDIA GeForce RTX 4050) 15-fb3130AX Gaming Laptop",
            "HP Victus Ryzen 5 7535HS 16GB 512GB RTX 3050",
            "HP Victus Ryzen 7 7840HS 16GB 1TB RTX 4050",
            "HP Victus generic laptop",
        ]

        ranked = [
            rank_flipkart_search_result(
                amazon,
                SimpleNamespace(title=title, position=index, pk=str(index)),
            )
            for index, title in enumerate(candidates)
        ]
        ranked.sort(key=lambda result: result["score"], reverse=True)

        self.assertGreaterEqual(ranked[0]["score"], 90)
        self.assertEqual(ranked[0]["candidate"].title, candidates[0])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_samsung_model_modifiers_are_hard_identity_signals(self):
        amazon = SimpleNamespace(
            product_title="Samsung Galaxy S25 Ultra 5G Smartphone 12GB RAM 256GB Storage Titanium Gray",
            brand="Samsung",
            processor="",
            ram="12 GB",
            storage="256 GB",
            operating_system="",
            color="Titanium Gray",
        )
        titles = {
            "ultra": "Samsung S25 Ultra 5G (Titanium Gray, 256 GB)",
            "edge": "Samsung Galaxy S25 Edge 5G (Titanium Black, 256 GB)",
            "fe": "Samsung Galaxy S25 FE 5G (Navy, 256 GB)",
            "base": "Samsung Galaxy S25 5G (Mint, 256 GB)",
            "generation": "Samsung Galaxy S24 Ultra 5G (Gray, 256 GB)",
        }
        results = {
            name: rank_flipkart_search_result(
                amazon,
                SimpleNamespace(title=title, position=index, pk=name),
            )
            for index, (name, title) in enumerate(titles.items())
        }

        self.assertEqual(results["ultra"]["match_status"], "matched")
        self.assertEqual(results["ultra"]["confidence"], "high")
        for name in ("edge", "fe", "base", "generation"):
            self.assertEqual(results[name]["match_status"], "rejected")
            self.assertIn("model", results[name]["signals"]["conflicts"])
        self.assertGreater(results["ultra"]["score"], results["edge"]["score"])

    def test_samsung_unknown_ram_is_not_a_conflict(self):
        amazon = SimpleNamespace(
            product_title="Samsung Galaxy S25 Ultra 256GB Titanium Gray",
            brand="Samsung", processor="", ram="12 GB", storage="256 GB",
            operating_system="", color="Titanium Gray",
        )
        result = rank_flipkart_search_result(
            amazon,
            SimpleNamespace(
                title="Samsung S25 Ultra 5G (Titanium Gray, 256 GB)",
                position=1,
                pk="unknown-ram",
            ),
        )
        self.assertEqual(result["match_status"], "matched")
        self.assertNotIn("ram", result["signals"]["conflicts"])

    def test_samsung_color_prefers_exact_variant_without_rejecting_other_color(self):
        amazon = SimpleNamespace(
            product_title="Samsung Galaxy S25 Ultra 256GB Titanium Gray",
            brand="Samsung", processor="", ram="12 GB", storage="256 GB",
            operating_system="", color="Titanium Gray",
        )
        gray = rank_flipkart_search_result(
            amazon, SimpleNamespace(title="Samsung S25 Ultra (Titanium Gray, 256 GB)", position=1, pk="gray")
        )
        black = rank_flipkart_search_result(
            amazon, SimpleNamespace(title="Samsung S25 Ultra (Titanium Black, 256 GB)", position=2, pk="black")
        )
        self.assertEqual(gray["match_status"], "matched")
        self.assertEqual(black["match_status"], "matched")
        self.assertGreater(gray["score"], black["score"])

    def test_exact_victus_title_matches_even_without_structured_amazon_specs(self):
        amazon = SimpleNamespace(
            product_title=(
                "HP Victus, AMD Ryzen 7 7445HS, 6GB RTX 4050, "
                "16GB DDR5(Upgradeable) 512GB SSD, 144Hz, IPS, 300 nits, "
                "15.6''/39.6cm, Win11, Office24, Blue, 2.29kg, fb3130AX, "
                "DTS Audio, Xbox Gamepass*, Gaming Laptop"
            ),
            brand="",
            processor="",
            ram="",
            storage="",
            operating_system="",
        )
        candidate = SimpleNamespace(
            title=(
                "HP Victus AMD Ryzen 7 Hexa Core 7445HS - "
                "(16 GB/512 GB SSD/Windows 11 Home/6 GB Graphics/"
                "NVIDIA GeForce RTX 4050) Gaming Laptop"
            ),
            position=1,
            pk="victus",
        )

        result = rank_flipkart_search_result(amazon, candidate)

        self.assertGreaterEqual(result["score"], 90)
        self.assertEqual(result["confidence"], "high")

    def test_model_prefix_and_marketing_word_normalization_match(self):
        amazon = SimpleNamespace(
            product_title="HP Victus Ryzen 7 7445HS 16GB 512GB SSD RTX 4050 fb3130AX",
            brand="HP",
            processor="",
            ram="",
            storage="",
            operating_system="",
        )
        candidate = SimpleNamespace(
            title="HP Victus AMD Ryzen 7 Hexa Core 7445HS 16 GB/512 GB SSD 6 GB Graphics RTX 4050 15-fb3130AX",
            position=1,
            pk="prefixed-model",
        )

        result = rank_flipkart_search_result(amazon, candidate)

        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["match_status"], "matched")
        self.assertTrue(result["signals"]["model_match"])
        self.assertFalse(result["signals"]["conflicts"])

    def test_core_spec_conflicts_reject_candidate(self):
        amazon = SimpleNamespace(
            product_title="HP Victus Ryzen 7 7445HS 16GB 512GB SSD RTX 4050 fb3130AX",
            brand="HP",
            processor="Ryzen 7 7445HS",
            ram="16 GB",
            storage="512 GB SSD",
            operating_system="Windows 11",
        )
        conflicting_titles = (
            "HP Victus Ryzen 7 7445HS 16GB 512GB SSD RTX 4060 fb3130AX",
            "HP Victus Ryzen 7 7445HS 8GB 512GB SSD RTX 4050 fb3130AX",
            "HP Victus Ryzen 5 7535HS 16GB 512GB SSD RTX 4050 fb3130AX",
            "HP Victus Ryzen 7 7445HS 16GB 1TB SSD RTX 4050 fb3130AX",
        )

        for title in conflicting_titles:
            with self.subTest(title=title):
                result = rank_flipkart_search_result(
                    amazon,
                    SimpleNamespace(title=title, position=1, pk=title),
                )
                self.assertEqual(result["match_status"], "rejected")
                self.assertEqual(result["confidence"], "low")
                self.assertTrue(result["signals"]["conflicts"])

    def test_missing_optional_spec_does_not_reject_identity_match(self):
        amazon = SimpleNamespace(
            product_title="HP Victus Ryzen 7 7445HS 16GB 512GB SSD fb3130AX",
            brand="HP",
            processor="Ryzen 7 7445HS",
            ram="16 GB",
            storage="512 GB SSD",
            operating_system="Windows 11",
        )
        candidate = SimpleNamespace(
            title="HP Victus AMD Ryzen 7 Hexa Core 7445HS 16 GB/512 GB SSD 15-fb3130AX",
            position=1,
            pk="missing-gpu",
        )

        result = rank_flipkart_search_result(amazon, candidate)

        self.assertEqual(result["match_status"], "matched")
        self.assertEqual(result["confidence"], "high")

    def test_search_accessory_is_rejected_even_with_exact_model_tokens(self):
        amazon = SimpleNamespace(
            product_title="Samsung Galaxy S25 Ultra 12GB 256GB Titanium Gray",
            brand="Samsung",
            ram="12GB",
            storage="256GB",
        )
        candidate = SimpleNamespace(
            title="WELLDESIGN Back Camera Lens Glass Protector for Samsung Galaxy S25 Ultra 256GB",
            position=1,
            pk="accessory-search-result",
        )

        result = rank_flipkart_search_result(amazon, candidate)

        self.assertEqual(result["match_status"], "rejected")
        self.assertEqual(result["signals"]["conflicts"]["product_type"], ["accessory"])

    def test_truncated_victus_search_title_still_selects_six_gb_candidate(self):
        amazon = SimpleNamespace(
            product_title=(
                "HP Victus, AMD Ryzen 7 7445HS, 6GB RTX 4050, 16GB DDR5 "
                "512GB SSD, Win11, fb3130AX Gaming Laptop"
            ),
            brand="HP",
            processor="AMD Ryzen 7 7445HS",
            ram="16 GB",
            storage="512 GB",
            operating_system="Windows 11",
        )
        candidates = [
            SimpleNamespace(
                pid="FOURGB",
                title=(
                    "HP Victus AMD Ryzen 7 Hexa Core 7445HS - "
                    "(16 GB/512 GB SSD/Windows 11 Home/4 GB Graphics/"
                    "NVIDIA GeForc..."
                ),
                position=3,
                pk="fourgb",
            ),
            SimpleNamespace(
                pid="SIXGB",
                title=(
                    "HP Victus AMD Ryzen 7 Hexa Core 7445HS - "
                    "(16 GB/512 GB SSD/Windows 11 Home/6 GB Graphics/"
                    "NVIDIA GeForc..."
                ),
                position=4,
                pk="sixgb",
            ),
        ]

        ranked = rank_flipkart_search_results(amazon, candidates)

        self.assertEqual(ranked[0]["candidate"].pid, "SIXGB")
        self.assertGreaterEqual(ranked[0]["score"], 90)
        self.assertEqual(ranked[0]["confidence"], "high")
        self.assertEqual(ranked[1]["signals"]["conflicts"]["gpu_memory"], ["4gb"])

    @patch("apps.importer.services.best_flipkart_match.search_and_save_flipkart_candidates")
    @patch("apps.importer.services.flipkart_product.extract_flipkart_product")
    def test_best_match_updates_existing_pid_and_product_match(self, extractor, search):
        amazon = self.make_best_match_product(asin="B0BEST000002")
        candidate = self.make_best_match_candidate(
            amazon, "EXIST123", "Apple iPhone 16 128 GB", 1
        )
        existing_product = FlipkartProduct.objects.create(
            search_result=candidate,
            pid=candidate.pid,
            url=candidate.product_url,
            product_title="Old title",
        )
        ProductMatch.objects.create(
            amazon_product=amazon,
            flipkart_product=existing_product,
            confidence="high",
            match_status="approved",
            score=1,
        )
        search.return_value = SimpleNamespace(candidate_pids=(candidate.pid,))
        extractor.return_value = {**MOCK_FLIPKART_PRODUCT, "pid": candidate.pid}

        extract_best_matched_flipkart_product(amazon)

        self.assertEqual(FlipkartProduct.objects.filter(pid=candidate.pid).count(), 1)
        existing_product.refresh_from_db()
        self.assertEqual(existing_product.product_title, MOCK_FLIPKART_PRODUCT["product_title"])
        match = ProductMatch.objects.get(amazon_product=amazon)
        self.assertEqual(match.match_status, "approved")
        self.assertGreater(match.score, 1)

    @patch("apps.importer.services.best_flipkart_match.search_and_save_flipkart_candidates")
    def test_best_match_skips_without_candidates_or_high_confidence(self, search):
        no_candidates = self.make_best_match_product(asin="B0BEST000003")
        search.return_value = SimpleNamespace(candidate_pids=())
        result = extract_best_matched_flipkart_product(no_candidates)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no Flipkart search candidates", result["reason"])

        weak = self.make_best_match_product(asin="B0BEST000004")
        weak_candidate = self.make_best_match_candidate(
            weak, "WEAK123", "Samsung Galaxy Tablet", 1
        )
        search.return_value = SimpleNamespace(candidate_pids=(weak_candidate.pid,))
        result = extract_best_matched_flipkart_product(weak)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no sufficiently matched", result["reason"])
        self.assertEqual(FlipkartProduct.objects.count(), 0)

    def test_amazon_product_admin_best_match_action_exists(self):
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        self.assertIn("extract_best_matched_flipkart_product", model_admin.actions)

    @patch("apps.importer.admin.extract_best_matched_flipkart_product_task")
    def test_best_match_admin_action_queues_each_product(self, task):
        first = self.make_best_match_product(asin="B0BEST000005")
        second = self.make_best_match_product(asin="B0BEST000006")
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="best-match-admin")

        with patch.object(model_admin, "message_user"):
            model_admin.extract_best_matched_flipkart_product(
                request,
                AmazonProduct.objects.filter(pk__in=[first.pk, second.pk]).order_by("asin"),
            )

        self.assertEqual(task.delay.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in task.delay.call_args_list},
            {str(first.pk), str(second.pk)},
        )

    @patch("apps.importer.services.best_flipkart_match.extract_best_matched_flipkart_product")
    def test_best_match_task_loads_product_id_and_reuses_service(self, service):
        amazon = self.make_best_match_product(asin="B0BEST000007")
        service.return_value = {"status": "skipped", "reason": "no match"}

        result = best_match_task.run(str(amazon.pk))

        self.assertEqual(result["status"], "skipped")
        service.assert_called_once_with(AmazonProduct.objects.get(pk=amazon.pk))
        amazon.refresh_from_db()
        self.assertEqual(amazon.status, ImportStatus.COMPLETED)

    @patch("apps.importer.services.best_flipkart_match.extract_best_matched_flipkart_product")
    def test_best_match_task_marks_source_failed_on_service_error(self, service):
        amazon = self.make_best_match_product(asin="B0BEST000008")
        service.side_effect = RuntimeError("Flipkart unavailable")

        with self.assertRaises(RuntimeError):
            best_match_task.run(str(amazon.pk))

        amazon.refresh_from_db()
        self.assertEqual(amazon.status, ImportStatus.FAILED)
        self.assertIn("Flipkart unavailable", amazon.error_message)

    def test_amazon_search_result_admin_queues_all_selected_results(self):
        keyword = SearchKeyword.objects.create(keyword="laptop")
        results = []
        for index in range(20):
            asin = f"B0ADMIN{index:06d}"
            results.append(
                AmazonSearchResult.objects.create(
                    keyword=keyword,
                    asin=asin,
                    title=f"Laptop {index}",
                    product_url=f"https://www.amazon.in/dp/{asin}",
                    position=index + 1,
                )
            )
        model_admin = AmazonSearchResultAdmin(AmazonSearchResult, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="amazon-admin")

        with patch("apps.importer.admin.amazon_product_extraction_task") as task:
            with patch.object(model_admin, "message_user"):
                model_admin.extract_amazon_products(
                    request,
                    AmazonSearchResult.objects.filter(keyword=keyword),
                )

        self.assertEqual(task.delay.call_count, 20)
        self.assertEqual({call.args[0] for call in task.delay.call_args_list}, {str(result.pk) for result in results})

    def test_amazon_search_result_admin_reports_queued(self):
        keyword = SearchKeyword.objects.create(keyword="laptop")
        result = AmazonSearchResult.objects.create(
            keyword=keyword,
            asin="B0ADMIN00004",
            title="Laptop",
            product_url="https://www.amazon.in/dp/B0ADMIN00004",
            position=1,
        )
        model_admin = AmazonSearchResultAdmin(AmazonSearchResult, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="extract-admin")

        with patch("apps.importer.admin.amazon_product_extraction_task") as task:
            with patch.object(model_admin, "message_user") as message:
                model_admin.extract_amazon_products(
                    request,
                    AmazonSearchResult.objects.filter(pk=result.pk),
                )
        task.delay.assert_called_once_with(str(result.pk), job_id=ANY)
        self.assertTrue(any("queued" in str(call).lower() for call in message.call_args_list))

    def test_amazon_search_result_admin_does_not_extract_in_request(self):
        keyword = SearchKeyword.objects.create(keyword="laptop")
        result = AmazonSearchResult.objects.create(
            keyword=keyword,
            asin="B0ADMIN00005",
            title="Laptop",
            product_url="https://www.amazon.in/dp/B0ADMIN00005",
            position=1,
        )
        model_admin = AmazonSearchResultAdmin(AmazonSearchResult, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="failure-admin")

        with patch("apps.importer.admin.amazon_product_extraction_task") as task:
            with patch.object(model_admin, "message_user") as message:
                model_admin.extract_amazon_products(
                    request,
                    AmazonSearchResult.objects.filter(pk=result.pk),
                )

        task.delay.assert_called_once_with(str(result.pk), job_id=ANY)
        self.assertTrue(any("queued" in str(call).lower() for call in message.call_args_list))

    def test_repeating_admin_extraction_queues_same_result_id(self):
        result = ImporterTests().make_search_result(asin="B0ADMIN00006")
        model_admin = AmazonSearchResultAdmin(AmazonSearchResult, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="repeat-admin")
        queryset = AmazonSearchResult.objects.filter(pk=result.pk)

        with patch("apps.importer.admin.amazon_product_extraction_task") as task:
            with patch.object(model_admin, "message_user"):
                model_admin.extract_amazon_products(request, queryset)
                model_admin.extract_amazon_products(request, queryset)
        self.assertEqual(task.delay.call_count, 2)
        self.assertEqual(task.delay.call_args_list[0].args, (str(result.pk),))

    def test_amazon_admin_search_action_queues_product_id(self):
        amazon = AmazonProduct.objects.create(
            asin="B0ADMIN00001",
            product_title="Apple iPhone Air",
            brand="Apple",
            url="https://www.amazon.in/dp/B0ADMIN00001",
            status=ImportStatus.COMPLETED,
        )
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        with patch("apps.importer.admin.flipkart_product_search_task") as task:
            with patch.object(model_admin, "message_user"):
                model_admin.search_flipkart(
                    self.factory.post("/admin/"),
                    AmazonProduct.objects.filter(pk=amazon.pk),
                )
        task.delay.assert_called_once_with(str(amazon.pk), job_id=ANY)

    def test_amazon_product_admin_flipkart_action_exists(self):
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)

        self.assertIn("search_flipkart", model_admin.actions)

    def test_amazon_product_admin_search_queues_without_running_browser(self):
        amazon = AmazonProduct.objects.create(
            asin="B0ADMIN00007",
            product_title="Apple iPhone Air 256 GB Light Gold",
            brand="Apple",
            storage="256 GB",
            color="Light Gold",
            url="https://www.amazon.in/dp/B0ADMIN00007",
            status=ImportStatus.COMPLETED,
        )
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="flipkart-admin")

        with patch("apps.importer.admin.flipkart_product_search_task") as task:
            with patch.object(model_admin, "message_user"):
                model_admin.search_flipkart(
                    request,
                    AmazonProduct.objects.filter(pk=amazon.pk),
                )
        task.delay.assert_called_once_with(str(amazon.pk), job_id=ANY)
        self.assertEqual(FlipkartSearchResult.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductPrice.objects.count(), 0)

    def test_amazon_product_admin_queues_all_selected_products(self):
        products = []
        for index in range(22):
            products.append(
                AmazonProduct.objects.create(
                    asin=f"B0BATCH{index:06d}",
                    product_title=f"Laptop {index}",
                    brand="Example",
                    url=f"https://www.amazon.in/dp/B0BATCH{index:06d}",
                    status=ImportStatus.COMPLETED,
                )
            )
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="batch-admin")

        with patch("apps.importer.admin.flipkart_product_search_task") as task:
            with patch.object(model_admin, "message_user") as message:
                model_admin.search_flipkart(
                    request,
                    AmazonProduct.objects.filter(pk__in=[p.pk for p in products]),
                )

        self.assertEqual(task.delay.call_count, 22)
        self.assertTrue(any("queued" in str(call).lower() for call in message.call_args_list))

    def test_flipkart_search_queue_failure_does_not_stop_batch(self):
        first = AmazonProduct.objects.create(
            asin="B0BATCHFAIL1",
            product_title="Laptop 1",
            url="https://www.amazon.in/dp/B0BATCHFAIL1",
            status=ImportStatus.COMPLETED,
        )
        second = AmazonProduct.objects.create(
            asin="B0BATCHFAIL2",
            product_title="Laptop 2",
            url="https://www.amazon.in/dp/B0BATCHFAIL2",
            status=ImportStatus.COMPLETED,
        )
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = self.factory.post("/admin/")
        request.user = get_user_model().objects.create_user(username="failure-batch-admin")

        with patch("apps.importer.admin.flipkart_product_search_task") as task:
            task.delay.side_effect = [RuntimeError("queue unavailable"), None]
            with patch.object(model_admin, "message_user") as message:
                model_admin.search_flipkart(
                    request,
                    AmazonProduct.objects.filter(pk__in=[first.pk, second.pk]).order_by("asin"),
                )

        self.assertEqual(task.delay.call_count, 2)
        self.assertTrue(any("B0BATCHFAIL1" in str(call) for call in message.call_args_list))
        self.assertTrue(any("queue" in str(call).lower() for call in message.call_args_list))

    def test_flipkart_result_admin_extraction_action_calls_existing_service(self):
        amazon = AmazonProduct.objects.create(
            asin="B0ADMIN00002",
            product_title="Apple iPhone Air",
            url="https://www.amazon.in/dp/B0ADMIN00002",
            status=ImportStatus.COMPLETED,
        )
        result = FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid="MOBADMIN00002",
            title="Apple iPhone Air",
            product_url="https://www.flipkart.com/apple/p/itm?pid=MOBADMIN00002",
            position=1,
        )
        model_admin = FlipkartSearchResultAdmin(FlipkartSearchResult, admin.site)
        with patch("apps.importer.admin.flipkart_product_extraction_task") as task:
            with patch.object(model_admin, "message_user"):
                model_admin.extract_flipkart_products(
                    self.factory.post("/admin/"),
                    FlipkartSearchResult.objects.filter(pk=result.pk),
                )
        task.delay.assert_called_once_with(str(result.pk), job_id=ANY)

    def test_matching_admin_action_uses_service_and_updates_existing_match(self):
        amazon, flipkart = ImporterTests().make_matching_pair()
        product_match = ProductMatch.objects.create(
            amazon_product=amazon,
            flipkart_product=flipkart,
            score=1,
            confidence="low",
            match_status="review",
            reasons={},
        )
        result = {
            "score": 96,
            "confidence": "high",
            "match_status": "matched",
            "reasons": {"model": {"matched": True, "score": 35}},
        }
        model_admin = ProductMatchAdmin(ProductMatch, admin.site)
        with patch("apps.importer.admin.match_products", return_value=result) as matcher:
            with patch.object(model_admin, "message_user"):
                model_admin.run_product_matching(
                    self.factory.post("/admin/"),
                    ProductMatch.objects.filter(pk=product_match.pk),
                )
        matcher.assert_called_once_with(amazon, flipkart)
        product_match.refresh_from_db()
        self.assertEqual(product_match.score, 96)
        self.assertEqual(product_match.match_status, "matched")
        self.assertEqual(ProductMatch.objects.count(), 1)

    def test_admin_actions_report_failures_without_crashing(self):
        amazon = AmazonProduct.objects.create(
            asin="B0ADMIN00003",
            product_title="Apple iPhone Air",
            url="https://www.amazon.in/dp/B0ADMIN00003",
            status=ImportStatus.COMPLETED,
        )
        result = FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid="MOBADMIN00003",
            title="Apple iPhone Air",
            product_url="https://www.flipkart.com/apple/p/itm?pid=MOBADMIN00003",
            position=1,
        )
        model_admin = FlipkartSearchResultAdmin(FlipkartSearchResult, admin.site)
        with patch("apps.importer.admin.flipkart_product_extraction_task") as task:
            with patch.object(model_admin, "message_user") as message:
                model_admin.extract_flipkart_products(
                    self.factory.post("/admin/"),
                    FlipkartSearchResult.objects.filter(pk=result.pk),
                )
        task.delay.assert_called_once_with(str(result.pk), job_id=ANY)
        self.assertTrue(message.called)


class ProductPublishingTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Mobile Phones",
            slug="mobile-phones",
            is_active=True,
        )
        self.user = get_user_model().objects.create_user(
            username="publisher-admin",
            password="test-password",
        )

    def make_publishable_match(self, amazon_price=69999, flipkart_price=68499):
        amazon = AmazonProduct.objects.create(
            asin="B0PUBLISH0001",
            product_title="Apple iPhone Air 256 GB Light Gold",
            brand="Apple",
            storage="256 GB",
            color="Light Gold",
            current_selling_price_inr=amazon_price,
            mrp_inr=79999,
            url="https://www.amazon.in/dp/B0PUBLISH0001",
            status=ImportStatus.COMPLETED,
        )
        search_result = FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid="MOBPUBLISH0001",
            title="Apple iPhone Air (Light Gold, 256 GB)",
            product_url="https://www.flipkart.com/apple/p/itm?pid=MOBPUBLISH0001",
            position=1,
            processed=True,
        )
        flipkart = FlipkartProduct.objects.create(
            search_result=search_result,
            pid="MOBPUBLISH0001",
            product_title="Apple iPhone Air (Light Gold, 256 GB)",
            brand="Apple",
            storage="256 GB",
            current_selling_price_inr=flipkart_price,
            mrp_inr=79999,
            url=search_result.product_url,
            status=ImportStatus.COMPLETED,
        )
        return ProductMatch.objects.create(
            amazon_product=amazon,
            flipkart_product=flipkart,
            score=96,
            confidence="high",
            match_status="approved",
            publish_category=self.category,
        )

    def test_review_match_cannot_publish_automatically(self):
        product_match = self.make_publishable_match()
        product_match.match_status = "review"
        product_match.save(update_fields=["match_status"])
        with self.assertRaises(ValueError):
            publish_product_match(product_match)
        self.assertEqual(Product.objects.count(), 0)

    def test_approved_match_creates_product_and_both_prices(self):
        product_match = self.make_publishable_match()

        result = publish_product_match(product_match, user=self.user)

        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductPrice.objects.filter(product=result.product).count(), 2)
        self.assertEqual(
            ProductPrice.objects.get(product=result.product, platform="AMAZON").price,
            69999,
        )
        self.assertEqual(
            ProductPrice.objects.get(product=result.product, platform="FLIPKART").price,
            68499,
        )
        product_match.refresh_from_db()
        self.assertEqual(product_match.match_status, "published")
        self.assertEqual(product_match.approved_by, None)
        self.assertEqual(product_match.published_by, self.user)
        self.assertIsNotNone(product_match.published_at)
        self.assertEqual(product_match.published_product, result.product)

    def test_admin_approval_stores_approver_and_publishes(self):
        product_match = self.make_publishable_match()
        from .admin import ProductMatchAdmin

        model_admin = ProductMatchAdmin(ProductMatch, admin.site)
        request = RequestFactory().post("/admin/")
        request.user = self.user
        with patch.object(model_admin, "message_user"):
            model_admin.approve_and_publish(
                request,
                ProductMatch.objects.filter(pk=product_match.pk),
            )
        product_match.refresh_from_db()
        self.assertEqual(product_match.match_status, "published")
        self.assertEqual(product_match.approved_by, self.user)
        self.assertEqual(product_match.published_by, self.user)
        self.assertIsNotNone(product_match.approved_at)

    def test_admin_without_category_stays_in_review(self):
        product_match = self.make_publishable_match()
        product_match.match_status = "review"
        product_match.publish_category = None
        product_match.save(update_fields=["match_status", "publish_category"])
        model_admin = ProductMatchAdmin(ProductMatch, admin.site)
        request = RequestFactory().post("/admin/")
        request.user = self.user

        with patch.object(model_admin, "message_user") as message:
            model_admin.approve_and_publish(
                request,
                ProductMatch.objects.filter(pk=product_match.pk),
            )

        product_match.refresh_from_db()
        self.assertEqual(product_match.match_status, "review")
        self.assertTrue(
            any("select at least one active category" in str(call) for call in message.call_args_list)
        )
        self.assertEqual(Product.objects.count(), 0)

    def test_inactive_category_cannot_publish(self):
        product_match = self.make_publishable_match()
        self.category.is_active = False
        self.category.save(update_fields=["is_active"])

        with self.assertRaises(ValueError) as context:
            publish_product_match(product_match)

        self.assertIn("inactive", str(context.exception).lower())
        self.assertEqual(Product.objects.count(), 0)

    def test_product_match_form_only_offers_active_categories(self):
        inactive = Category.objects.create(
            name="Inactive Laptops",
            slug="inactive-laptops",
            is_active=False,
        )
        product_match = self.make_publishable_match()
        form = ProductMatchAdminForm(instance=product_match)
        category_ids = set(form.fields["publish_category"].queryset.values_list("pk", flat=True))

        self.assertIn(self.category.pk, category_ids)
        self.assertNotIn(inactive.pk, category_ids)

    def test_multiple_active_categories_use_first_as_primary(self):
        second = Category.objects.create(
            name="Accessories",
            slug="accessories",
            is_active=True,
        )
        product_match = self.make_publishable_match()
        product_match.publish_categories.set([second, self.category])

        result = publish_product_match(product_match)

        self.assertEqual(result.product.category_id, second.pk)
        self.assertEqual(
            set(product_match.publish_categories.values_list("pk", flat=True)),
            {self.category.pk, second.pk},
        )

    def test_bulk_category_assignment_saves_selected_active_categories(self):
        product_match = self.make_publishable_match()
        second = Category.objects.create(
            name="Gaming",
            slug="gaming",
            is_active=True,
        )
        model_admin = ProductMatchAdmin(ProductMatch, admin.site)
        request = RequestFactory().post(
            "/admin/",
            {
                "apply_categories": "1",
                "categories": [str(self.category.pk), str(second.pk)],
            },
        )
        request.user = self.user

        with patch.object(model_admin, "message_user"):
            model_admin.assign_publish_categories(
                request,
                ProductMatch.objects.filter(pk=product_match.pk),
            )

        product_match.refresh_from_db()
        self.assertEqual(product_match.publish_category_id, second.pk)
        self.assertEqual(
            set(product_match.publish_categories.values_list("pk", flat=True)),
            {self.category.pk, second.pk},
        )

    def test_selected_category_is_assigned_to_existing_product(self):
        product_match = self.make_publishable_match()
        other_category = Category.objects.create(
            name="Computers",
            slug="computers",
            is_active=True,
        )
        existing = Product.objects.create(
            category=other_category,
            title="Apple iPhone Air",
            slug="apple-iphone-air",
            brand="Apple",
        )
        product_match.published_product = existing
        product_match.save(update_fields=["published_product"])

        publish_product_match(product_match)

        existing.refresh_from_db()
        self.assertEqual(existing.category_id, self.category.pk)

    def test_existing_product_is_reused(self):
        product_match = self.make_publishable_match()
        existing = Product.objects.create(
            category=self.category,
            title="Apple iPhone Air",
            slug="apple-iphone-air",
            brand="Apple",
        )

        result = publish_product_match(product_match)

        self.assertEqual(result.product.pk, existing.pk)
        self.assertEqual(Product.objects.count(), 1)

    def test_existing_prices_are_updated_without_duplicates(self):
        product_match = self.make_publishable_match()
        existing = Product.objects.create(
            category=self.category,
            title="Apple iPhone Air",
            slug="apple-iphone-air",
            brand="Apple",
        )
        product_match.published_product = existing
        product_match.save(update_fields=["published_product"])
        ProductPrice.objects.create(
            product=existing,
            platform="AMAZON",
            price=65000,
            affiliate_url="https://www.amazon.in/old",
        )
        ProductPrice.objects.create(
            product=existing,
            platform="FLIPKART",
            price=64000,
            affiliate_url="https://www.flipkart.com/old",
        )

        publish_product_match(product_match)

        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductPrice.objects.filter(product=existing).count(), 2)
        self.assertEqual(
            ProductPrice.objects.get(product=existing, platform="AMAZON").price,
            69999,
        )

    def test_null_price_does_not_overwrite_existing_price(self):
        product_match = self.make_publishable_match()
        existing = Product.objects.create(
            category=self.category,
            title="Apple iPhone Air",
            slug="apple-iphone-air",
            brand="Apple",
        )
        product_match.published_product = existing
        product_match.save(update_fields=["published_product"])
        ProductPrice.objects.create(
            product=existing,
            platform="AMAZON",
            price=65000,
            affiliate_url="https://www.amazon.in/old",
        )
        product_match.amazon_product.current_selling_price_inr = None
        product_match.amazon_product.save(update_fields=["current_selling_price_inr"])

        publish_product_match(product_match)

        self.assertEqual(
            ProductPrice.objects.get(product=existing, platform="AMAZON").price,
            65000,
        )
        self.assertEqual(ProductPrice.objects.filter(product=existing).count(), 2)

    def test_repeated_publish_is_idempotent(self):
        product_match = self.make_publishable_match()
        first = publish_product_match(product_match)
        second = publish_product_match(product_match)

        self.assertEqual(first.product.pk, second.product.pk)
        self.assertTrue(second.already_published)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductPrice.objects.count(), 2)

    def test_rejected_match_cannot_publish(self):
        product_match = self.make_publishable_match()
        product_match.match_status = "rejected"
        product_match.save(update_fields=["match_status"])

        with self.assertRaises(ValueError):
            publish_product_match(product_match)
        self.assertEqual(Product.objects.count(), 0)

    def test_missing_category_fails_without_creating_live_data(self):
        product_match = self.make_publishable_match()
        product_match.publish_category = None
        product_match.save(update_fields=["publish_category"])

        with self.assertRaises(ValueError) as context:
            publish_product_match(product_match)

        self.assertIn("category", str(context.exception).lower())
        self.assertEqual(Product.objects.count(), 0)

    def test_price_failure_rolls_back_product_and_prices(self):
        product_match = self.make_publishable_match()
        product_match.flipkart_product.url = ""
        product_match.flipkart_product.save(update_fields=["url"])

        with self.assertRaises(ValueError):
            publish_product_match(product_match)

        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductPrice.objects.count(), 0)
        product_match.refresh_from_db()
        self.assertEqual(product_match.match_status, "publish_failed")
        self.assertTrue(product_match.publish_error)

    def test_publisher_makes_no_network_calls(self):
        product_match = self.make_publishable_match()
        with patch("apps.importer.services.flipkart_product.extract_flipkart_product") as flipkart:
            with patch("apps.importer.services.amazon_product.extract_amazon_product") as amazon:
                publish_product_match(product_match)
        flipkart.assert_not_called()
        amazon.assert_not_called()


class KeywordProductMatchingAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="matching-admin")

    def make_pair(self, keyword_text, asin, pid, title):
        keyword = SearchKeyword.objects.get_or_create(keyword=keyword_text)[0]
        amazon = AmazonProduct.objects.create(
            asin=asin,
            product_title=title,
            brand="Apple" if "iPhone" in title else "Dell",
            storage="256 GB",
            url=f"https://www.amazon.in/dp/{asin}",
            status=ImportStatus.COMPLETED,
        )
        AmazonSearchResult.objects.create(
            keyword=keyword,
            asin=asin,
            title=title,
            product_url=amazon.url,
            position=1,
            processed=True,
        )
        search_result = FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid=pid,
            title=title,
            product_url=f"https://www.flipkart.com/product/p/itm?pid={pid}",
            position=1,
            processed=True,
        )
        flipkart = FlipkartProduct.objects.create(
            search_result=search_result,
            pid=pid,
            product_title=title,
            brand=amazon.brand,
            storage="256GB",
            url=search_result.product_url,
            status=ImportStatus.COMPLETED,
        )
        return keyword, amazon, flipkart

    def test_search_keyword_admin_matching_action_exists(self):
        model_admin = admin.site._registry[SearchKeyword]

        self.assertIn("run_product_matching", model_admin.actions)
        self.assertIn("run_flipkart_search", model_admin.actions)

    def test_search_keyword_admin_flipkart_action_queues_task_only(self):
        keyword, amazon, _ = self.make_pair(
            "laptop", "B0LAPTOP0003", "MOBLAPTOP003", "Dell Laptop 256 GB"
        )
        model_admin = admin.site._registry[SearchKeyword]
        request = self.factory.post("/admin/")
        request.user = self.user

        with patch("apps.importer.admin.flipkart_search_task") as flipkart_search:
            with patch("apps.importer.admin.run_flipkart_search_for_keyword") as sync_search:
                with patch.object(model_admin, "message_user") as message:
                    model_admin.run_flipkart_search(
                        request,
                        SearchKeyword.objects.filter(pk=keyword.pk),
                    )

        flipkart_search.delay.assert_called_once_with(str(keyword.pk), job_id=ANY)
        sync_search.assert_not_called()
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductPrice.objects.count(), 0)
        self.assertTrue(any("queued" in str(call).lower() for call in message.call_args_list))

    def test_search_keyword_admin_flipkart_action_processes_multiple_keywords(self):
        laptop_keyword, _, _ = self.make_pair(
            "laptop", "B0LAPTOP0004", "MOBLAPTOP004", "Dell Laptop 256 GB"
        )
        phone_keyword, _, _ = self.make_pair(
            "iphone", "B0PHONE00004", "MOBPHONE004", "Apple iPhone 256 GB"
        )
        model_admin = admin.site._registry[SearchKeyword]
        request = self.factory.post("/admin/")
        request.user = self.user

        with patch("apps.importer.admin.flipkart_search_task") as flipkart_search:
            with patch.object(model_admin, "message_user"):
                model_admin.run_flipkart_search(
                    request,
                    SearchKeyword.objects.filter(pk__in=[laptop_keyword.pk, phone_keyword.pk]),
                )

        self.assertEqual(flipkart_search.delay.call_count, 2)

    def test_search_keyword_admin_total_flipkart_results_uses_persisted_records(self):
        keyword, amazon, _ = self.make_pair(
            "iphone", "B0PHONE00011", "MOBPHONE011", "Apple iPhone 256 GB"
        )
        batch = ImportBatch.objects.create(
            keyword=keyword,
            status=BatchStatus.COMPLETED,
            amazon_products_count=1,
            flipkart_results_count=2,
            successful_count=1,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        first = FlipkartSearchResult.objects.create(
            amazon_product=amazon,
            pid="MOBPHONE012",
            title="Apple iPhone 256 GB Variant",
            product_url="https://www.flipkart.com/product/p/itm?pid=MOBPHONE012",
            position=2,
            processed=False,
        )
        first.batches.add(batch)
        for result in amazon.flipkart_results.all():
            result.batches.add(batch)
        model_admin = admin.site._registry[SearchKeyword]

        self.assertEqual(model_admin.total_flipkart_results(keyword), 2)

    def test_search_keyword_admin_flipkart_summary_uses_running_batch_progress(self):
        keyword = SearchKeyword.objects.create(keyword="running-summary")
        source = AmazonProduct.objects.create(
            asin="FKKWTESTSUMMARY001",
            product_title="running-summary",
            url="https://www.flipkart.com/search?q=running-summary",
            status=ImportStatus.COMPLETED,
        )
        result = FlipkartSearchResult.objects.create(
            amazon_product=source,
            pid="MOBRUNSUMMARY1",
            title="Running Summary Laptop 1",
            product_url="https://www.flipkart.com/product/p/itm?pid=MOBRUNSUMMARY1",
            position=1,
            processed=False,
        )
        ImportBatch.objects.create(
            keyword=keyword,
            status=BatchStatus.FLIPKART_SEARCH,
            amazon_products_count=1,
            successful_count=1,
            failed_count=0,
            flipkart_results_count=1,
            started_at=timezone.now(),
        )
        result.batches.add(keyword.import_batches.first())
        model_admin = admin.site._registry[SearchKeyword]

        self.assertEqual(
            model_admin.flipkart_search_summary(keyword),
            "1/1 searched · 1 candidates · Running",
        )

    def test_flipkart_search_filter_matches_keyword_statuses(self):
        pending_keyword = SearchKeyword.objects.create(keyword="pending")
        completed_keyword, completed_amazon, _ = self.make_pair(
            "completed", "B0LAPTOP0005", "MOBLAPTOP005", "Dell Laptop 256 GB"
        )
        running_keyword = SearchKeyword.objects.create(keyword="running")
        failed_keyword = SearchKeyword.objects.create(keyword="failed")

        running_amazon = AmazonProduct.objects.create(
            asin="B0RUNNING001",
            product_title="Running Laptop",
            brand="Dell",
            storage="256 GB",
            url="https://www.amazon.in/dp/B0RUNNING001",
            status=ImportStatus.COMPLETED,
        )
        AmazonSearchResult.objects.create(
            keyword=running_keyword,
            asin=running_amazon.asin,
            title=running_amazon.product_title,
            product_url=running_amazon.url,
            position=1,
            processed=True,
        )
        second_running = AmazonProduct.objects.create(
            asin="B0RUNNING002",
            product_title="Running Laptop 2",
            brand="Dell",
            storage="256 GB",
            url="https://www.amazon.in/dp/B0RUNNING002",
            status=ImportStatus.COMPLETED,
        )
        AmazonSearchResult.objects.create(
            keyword=running_keyword,
            asin=second_running.asin,
            title=second_running.product_title,
            product_url=second_running.url,
            position=2,
            processed=True,
        )
        FlipkartSearchResult.objects.create(
            amazon_product=running_amazon,
            pid="MOBRUNNING001",
            title="Running Laptop",
            product_url="https://www.flipkart.com/product/p/itm?pid=MOBRUNNING001",
            position=1,
            processed=False,
        )

        failed_amazon = AmazonProduct.objects.create(
            asin="B0FAILED001",
            product_title="Failed Laptop",
            brand="Dell",
            storage="256 GB",
            url="https://www.amazon.in/dp/B0FAILED001",
            status=ImportStatus.COMPLETED,
        )
        AmazonSearchResult.objects.create(
            keyword=failed_keyword,
            asin=failed_amazon.asin,
            title=failed_amazon.product_title,
            product_url=failed_amazon.url,
            position=1,
            processed=True,
        )
        ImportBatch.objects.create(
            keyword=failed_keyword,
            status=BatchStatus.FAILED,
            error_message=f"Flipkart search for {failed_amazon.asin}: Flipkart timed out",
        )

        model_admin = admin.site._registry[SearchKeyword]

        def filtered_keywords(value):
            request = self.factory.get("/admin/importer/searchkeyword/", {"flipkart_search_status": value})
            filter_instance = FlipkartSearchStatusFilter(
                request,
                request.GET.copy(),
                SearchKeyword,
                model_admin,
            )
            return set(
                filter_instance.queryset(
                    request,
                    SearchKeyword.objects.order_by("keyword"),
                ).values_list("keyword", flat=True)
            )

        self.assertEqual(filtered_keywords(ImportStatus.PENDING), {"pending"})
        self.assertEqual(filtered_keywords(ImportStatus.COMPLETED), {"completed"})
        self.assertEqual(filtered_keywords(ImportStatus.RUNNING), {"running"})
        self.assertEqual(filtered_keywords(ImportStatus.FAILED), {"failed"})

    def test_matching_is_scoped_to_selected_keyword_and_uses_existing_records(self):
        laptop_keyword, laptop_amazon, laptop_flipkart = self.make_pair(
            "laptop", "B0LAPTOP0001", "MOBLAPTOP001", "Dell Laptop 256 GB"
        )
        phone_keyword, phone_amazon, phone_flipkart = self.make_pair(
            "iphone", "B0PHONE00001", "MOBPHONE001", "Apple iPhone 256 GB"
        )
        model_admin = admin.site._registry[SearchKeyword]
        request = self.factory.post("/admin/")
        request.user = self.user

        with patch("apps.importer.admin.search_and_save_flipkart_candidates") as flipkart_search:
            with patch("apps.importer.admin.process_amazon_search_result") as amazon_extract:
                with patch("apps.importer.admin.process_flipkart_search_result") as flipkart_extract:
                    with patch.object(model_admin, "message_user") as message:
                        model_admin.run_product_matching(
                            request,
                            SearchKeyword.objects.filter(pk=laptop_keyword.pk),
                        )

        flipkart_search.assert_not_called()
        amazon_extract.assert_not_called()
        flipkart_extract.assert_not_called()
        self.assertTrue(
            ProductMatch.objects.filter(
                amazon_product=laptop_amazon,
                flipkart_product=laptop_flipkart,
            ).exists()
        )
        self.assertFalse(
            ProductMatch.objects.filter(
                amazon_product=phone_amazon,
                flipkart_product=phone_flipkart,
            ).exists()
        )
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductPrice.objects.count(), 0)
        laptop_keyword.refresh_from_db()
        self.assertEqual(laptop_keyword.matching_status, ImportStatus.COMPLETED)
        self.assertTrue(any("Amazon products: 1" in str(call) for call in message.call_args_list))
        self.assertTrue(any("Flipkart products: 1" in str(call) for call in message.call_args_list))

    def test_matching_rerun_updates_one_product_match_without_duplicates(self):
        keyword, amazon, flipkart = self.make_pair(
            "laptop", "B0LAPTOP0002", "MOBLAPTOP002", "Dell Laptop 256 GB"
        )
        model_admin = admin.site._registry[SearchKeyword]
        request = self.factory.post("/admin/")
        request.user = self.user
        queryset = SearchKeyword.objects.filter(pk=keyword.pk)

        with patch.object(model_admin, "message_user"):
            model_admin.run_product_matching(request, queryset)
            model_admin.run_product_matching(request, queryset)

        self.assertEqual(
            ProductMatch.objects.filter(
                amazon_product=amazon,
                flipkart_product=flipkart,
            ).count(),
            1,
        )


class ImportBatchTests(TestCase):
    def make_batch(self, keyword="laptop"):
        search_keyword = SearchKeyword.objects.create(keyword=keyword)
        return ImportBatch.objects.create(keyword=search_keyword)

    def test_batch_creation(self):
        batch = self.make_batch()
        self.assertEqual(batch.status, BatchStatus.PENDING)
        self.assertEqual(batch.keyword.keyword, "laptop")

    def test_batch_runs_all_stages_with_existing_services_mocked(self):
        batch = self.make_batch()

        def search_amazon(keyword):
            for index in range(2):
                asin = f"B0BATCHRUN{index:04d}"
                AmazonSearchResult.objects.create(
                    keyword=batch.keyword,
                    asin=asin,
                    title=f"Dell Laptop {index} 256 GB",
                    product_url=f"https://www.amazon.in/dp/{asin}",
                    position=index + 1,
                )
            return SimpleNamespace()

        def extract_amazon(result):
            product = AmazonProduct.objects.create(
                asin=result.asin,
                product_title=result.title,
                brand="Dell",
                storage="256 GB",
                url=result.product_url,
                status=ImportStatus.COMPLETED,
            )
            result.processed = True
            result.save(update_fields=["processed"])
            return True

        def search_flipkart(product):
            result = FlipkartSearchResult.objects.create(
                amazon_product=product,
                pid=f"MOB{product.asin[-4:]}",
                title=product.product_title,
                product_url=f"https://www.flipkart.com/laptop/p/{product.asin}",
                position=1,
            )
            return SimpleNamespace(
                candidates_found=1,
                candidates_selected=1,
                saved=1,
                skipped_duplicates=0,
            )

        def extract_flipkart(result):
            FlipkartProduct.objects.create(
                search_result=result,
                pid=result.pid,
                product_title=result.title,
                brand="Dell",
                storage="256GB",
                url=result.product_url,
                status=ImportStatus.COMPLETED,
            )
            result.processed = True
            result.save(update_fields=["processed"])
            return True

        with patch("apps.importer.services.batch_runner.run_amazon_search_for_keyword", side_effect=search_amazon):
            with patch("apps.importer.services.batch_runner.process_amazon_search_result", side_effect=extract_amazon):
                with patch("apps.importer.services.batch_runner.search_and_save_flipkart_candidates", side_effect=search_flipkart):
                    with patch("apps.importer.services.batch_runner.process_flipkart_search_result", side_effect=extract_flipkart):
                        result = run_batch(batch)

        self.assertEqual(result.status, BatchStatus.READY_FOR_REVIEW)
        self.assertEqual(result.amazon_results_count, 2)
        self.assertEqual(result.amazon_products_count, 2)
        self.assertEqual(result.flipkart_results_count, 2)
        self.assertEqual(result.flipkart_products_count, 2)
        self.assertEqual(result.matches_count, 2)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.amazon_search_results.count(), 2)
        self.assertEqual(result.amazon_products.count(), 2)
        self.assertEqual(result.flipkart_search_results.count(), 2)
        self.assertEqual(result.flipkart_products.count(), 2)
        self.assertEqual(result.product_matches.count(), 2)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductPrice.objects.count(), 0)

    def test_batch_isolation_prevents_cross_batch_matches(self):
        batch_one = self.make_batch("laptop")
        batch_two = self.make_batch("iphone")
        amazon_one = AmazonProduct.objects.create(
            asin="B0ISOLATE01", product_title="Dell Laptop 256 GB", brand="Dell",
            storage="256 GB", url="https://www.amazon.in/dp/B0ISOLATE01", status=ImportStatus.COMPLETED,
        )
        amazon_two = AmazonProduct.objects.create(
            asin="B0ISOLATE02", product_title="Apple iPhone 256 GB", brand="Apple",
            storage="256 GB", url="https://www.amazon.in/dp/B0ISOLATE02", status=ImportStatus.COMPLETED,
        )
        for batch, amazon, pid in (
            (batch_one, amazon_one, "MOBISOLATE01"),
            (batch_two, amazon_two, "MOBISOLATE02"),
        ):
            amazon.batches.add(batch)
            result = FlipkartSearchResult.objects.create(
                amazon_product=amazon, pid=pid, title=amazon.product_title,
                product_url=f"https://www.flipkart.com/p/{pid}", position=1,
            )
            result.batches.add(batch)
            product = FlipkartProduct.objects.create(
                search_result=result, pid=pid, product_title=amazon.product_title,
                brand=amazon.brand, storage=amazon.storage, url=result.product_url,
                status=ImportStatus.COMPLETED,
            )
            product.batches.add(batch)

        summary = run_product_matching_for_batch(batch_one)

        self.assertEqual(summary.matches_created, 1)
        self.assertEqual(ProductMatch.objects.filter(batches=batch_one).count(), 1)
        self.assertEqual(ProductMatch.objects.filter(batches=batch_two).count(), 0)
        self.assertFalse(ProductMatch.objects.filter(amazon_product=amazon_two).exists())

    def test_batch_partial_failure_continues_and_is_ready_for_review(self):
        batch = self.make_batch()
        results = []
        for index in range(2):
            asin = f"B0PARTIAL{index:04d}"
            results.append(AmazonSearchResult.objects.create(
                keyword=batch.keyword, asin=asin, title=f"Laptop {index}",
                product_url=f"https://www.amazon.in/dp/{asin}", position=index + 1,
            ))
        batch.amazon_search_results.add(*results)

        def extract(result):
            if result.asin.endswith("0000"):
                raise RuntimeError("temporary Amazon failure")
            AmazonProduct.objects.create(
                asin=result.asin, product_title=result.title, brand="Dell",
                url=result.product_url, status=ImportStatus.COMPLETED,
            )
            result.processed = True
            result.save(update_fields=["processed"])
            return True

        with patch("apps.importer.services.batch_runner.process_amazon_search_result", side_effect=extract):
            with patch("apps.importer.services.batch_runner.search_and_save_flipkart_candidates"):
                with patch("apps.importer.services.batch_runner.process_flipkart_search_result"):
                    result = run_batch(batch)

        self.assertEqual(result.status, BatchStatus.READY_FOR_REVIEW)
        self.assertEqual(result.amazon_products_count, 1)
        self.assertGreaterEqual(result.failed_count, 1)
        self.assertIn("temporary Amazon failure", result.error_message)

    def test_batch_rerun_is_idempotent(self):
        batch = self.make_batch()
        batch.status = BatchStatus.READY_FOR_REVIEW
        batch.save(update_fields=["status"])

        with patch("apps.importer.services.batch_runner.run_amazon_search_for_keyword") as search:
            result = run_batch(batch)

        search.assert_not_called()
        self.assertEqual(result.status, BatchStatus.READY_FOR_REVIEW)

    def test_batch_admin_actions_are_registered(self):
        batch_admin = admin.site._registry[ImportBatch]
        for action in (
            "run_batch_action", "resume_batch_action", "cancel_batch_action",
            "review_matches_action", "publish_approved_products_action",
        ):
            self.assertIn(action, batch_admin.actions)


class MarketplaceIndependentPublishingTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Mobiles", slug="mobiles")
        self.amazon = AmazonProduct.objects.create(
            asin="B0INDEPENDENT1",
            product_title="Independent Phone",
            brand="Example",
            url="https://www.amazon.in/dp/B0INDEPENDENT1",
            status=ImportStatus.COMPLETED,
            current_selling_price_inr=50000,
            mrp_inr=60000,
        )
        self.flipkart_result = FlipkartSearchResult.objects.create(
            pid="MOBINDEPENDENT1",
            title="Independent Phone",
            product_url="https://www.flipkart.com/phone/p?pid=MOBINDEPENDENT1",
            position=1,
        )
        self.flipkart = FlipkartProduct.objects.create(
            search_result=self.flipkart_result,
            pid="MOBINDEPENDENT1",
            product_title="Independent Phone",
            brand="Example",
            url=self.flipkart_result.product_url,
            status=ImportStatus.COMPLETED,
            current_selling_price_inr=48000,
            mrp_inr=60000,
        )

    def test_amazon_only_product_can_be_published(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)

        product = publish_amazon_product(self.amazon)

        self.assertEqual(Product.objects.count(), 1)
        self.assertTrue(ProductPrice.objects.filter(product=product, platform="AMAZON").exists())
        self.assertFalse(ProductPrice.objects.filter(product=product, platform="FLIPKART").exists())

    def test_flipkart_only_product_can_be_published(self):
        self.flipkart.categories.set([self.category])
        approve_flipkart_product(self.flipkart)

        product = publish_flipkart_product(self.flipkart)

        self.assertEqual(Product.objects.count(), 1)
        self.assertFalse(ProductPrice.objects.filter(product=product, platform="AMAZON").exists())
        self.assertTrue(ProductPrice.objects.filter(product=product, platform="FLIPKART").exists())

    def test_flipkart_primary_image_is_transferred_without_download(self):
        self.flipkart.images = ["https://rukminim.example/phone.jpg"]
        self.flipkart.save(update_fields=["images", "updated_at"])
        self.flipkart.categories.set([self.category])
        approve_flipkart_product(self.flipkart)

        product = publish_flipkart_product(self.flipkart)

        self.assertEqual(product.marketplace_image_url, "https://rukminim.example/phone.jpg")

    def test_both_marketplaces_share_one_product(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        product = publish_amazon_product(self.amazon)

        associate_flipkart_product(self.flipkart, product)

        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductPrice.objects.filter(product=product).count(), 2)
        self.assertEqual(self.flipkart.published_product_id, product.pk)

    def test_marketplace_prices_update_independently(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        product = publish_amazon_product(self.amazon)
        associate_flipkart_product(self.flipkart, product)

        self.amazon.current_selling_price_inr = 47000
        self.amazon.save(update_fields=["current_selling_price_inr", "updated_at"])
        publish_amazon_product(self.amazon)
        self.assertEqual(
            ProductPrice.objects.get(product=product, platform="AMAZON").price,
            47000,
        )
        self.assertEqual(
            ProductPrice.objects.get(product=product, platform="FLIPKART").price,
            48000,
        )

        self.flipkart.current_selling_price_inr = 45000
        self.flipkart.save(update_fields=["current_selling_price_inr", "updated_at"])
        associate_flipkart_product(self.flipkart, product)
        self.assertEqual(
            ProductPrice.objects.get(product=product, platform="AMAZON").price,
            47000,
        )
        self.assertEqual(
            ProductPrice.objects.get(product=product, platform="FLIPKART").price,
            45000,
        )

    def test_unlinking_flipkart_keeps_canonical_product(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        product = publish_amazon_product(self.amazon)
        associate_flipkart_product(self.flipkart, product)

        unpublish_flipkart_product(self.flipkart)

        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertTrue(Product.objects.public().filter(pk=product.pk).exists())
        self.assertFalse(ProductPrice.objects.filter(product=product, platform="FLIPKART").exists())

    def test_public_cards_show_only_available_marketplaces_and_lowest_price(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        product = publish_amazon_product(self.amazon)
        from apps.products.services import build_product_card

        product.list_prices = list(product.prices.all())
        card = build_product_card(product, price_attribute="list_prices")
        self.assertTrue(card["has_amazon"])
        self.assertFalse(card["has_flipkart"])
        self.assertTrue(card["is_amazon_lowest"])

        associate_flipkart_product(self.flipkart, product)
        product.list_prices = list(product.prices.all())
        card = build_product_card(product, price_attribute="list_prices")
        self.assertTrue(card["has_amazon"])
        self.assertTrue(card["has_flipkart"])
        self.assertTrue(card["is_flipkart_lowest"])

        unpublish_flipkart_product(self.flipkart)
        product.list_prices = list(product.prices.all())
        card = build_product_card(product, price_attribute="list_prices")
        self.assertTrue(card["has_amazon"])
        self.assertFalse(card["has_flipkart"])

    def test_public_detail_handles_missing_marketplace_data(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        publish_amazon_product(self.amazon)

        response = self.client.get("/product/independent-phone/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Buy on Amazon")
        self.assertNotContains(response, "Buy on Flipkart")

    def test_flipkart_bulk_category_action_assigns_multiple_products_and_categories(self):
        second_result = FlipkartSearchResult.objects.create(
            pid="MOBINDEPENDENT2",
            title="Independent Tablet",
            product_url="https://www.flipkart.com/tablet/p?pid=MOBINDEPENDENT2",
            position=2,
        )
        second = FlipkartProduct.objects.create(
            search_result=second_result,
            pid="MOBINDEPENDENT2",
            product_title="Independent Tablet",
            url="https://www.flipkart.com/tablet/p?pid=MOBINDEPENDENT2",
            status=ImportStatus.COMPLETED,
        )
        second_category = Category.objects.create(name="Tablets", slug="tablets")
        model_admin = FlipkartProductAdmin(FlipkartProduct, admin.site)
        request = RequestFactory().post(
            "/admin/importer/flipkartproduct/assign-categories/",
            {
                "ids": f"{self.flipkart.pk},{second.pk}",
                "categories": [str(self.category.pk), str(second_category.pk)],
            },
        )
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)

        with patch.object(model_admin, "message_user"):
            response = model_admin.assign_categories_view(request)

        self.assertEqual(response.status_code, 302)
        expected = {self.category.pk, second_category.pk}
        self.assertEqual(set(self.flipkart.categories.values_list("pk", flat=True)), expected)
        self.assertEqual(set(second.categories.values_list("pk", flat=True)), expected)

    def test_flipkart_bulk_category_action_redirects_to_confirmation(self):
        model_admin = FlipkartProductAdmin(FlipkartProduct, admin.site)
        request = RequestFactory().post("/admin/importer/flipkartproduct/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)

        response = model_admin.assign_categories(
            request,
            FlipkartProduct.objects.filter(pk=self.flipkart.pk),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("assign-categories/", response.url)
        self.assertIn(str(self.flipkart.pk), response.url)

    def test_flipkart_bulk_category_action_rejects_empty_selection(self):
        model_admin = FlipkartProductAdmin(FlipkartProduct, admin.site)
        request = RequestFactory().post(
            "/admin/importer/flipkartproduct/assign-categories/",
            {"ids": str(self.flipkart.pk)},
        )
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)

        with patch.object(model_admin.admin_site, "each_context", return_value={}):
            response = model_admin.assign_categories_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertFalse(self.flipkart.categories.exists())

    def test_flipkart_bulk_category_assignment_preserves_existing_categories(self):
        second_category = Category.objects.create(name="Tablets", slug="tablets")
        self.flipkart.categories.add(self.category)

        assign_staged_product_categories(
            FlipkartProduct,
            [self.flipkart.pk],
            [second_category],
        )

        self.assertEqual(
            set(self.flipkart.categories.values_list("pk", flat=True)),
            {self.category.pk, second_category.pk},
        )

    def test_flipkart_bulk_category_assignment_rolls_back_on_failure(self):
        second_result = FlipkartSearchResult.objects.create(
            pid="MOBINDEPENDENT2",
            title="Independent Tablet",
            product_url="https://www.flipkart.com/tablet/p?pid=MOBINDEPENDENT2",
            position=2,
        )
        second = FlipkartProduct.objects.create(
            search_result=second_result,
            pid="MOBINDEPENDENT2",
            product_title="Independent Tablet",
            url="https://www.flipkart.com/tablet/p?pid=MOBINDEPENDENT2",
            status=ImportStatus.COMPLETED,
        )
        manager_class = type(self.flipkart.categories)
        with patch.object(
            manager_class,
            "add",
            side_effect=[None, RuntimeError("category assignment failed")],
        ):
            with self.assertRaises(RuntimeError):
                assign_staged_product_categories(
                    FlipkartProduct,
                    [self.flipkart.pk, second.pk],
                    [self.category],
                )

        self.assertFalse(self.flipkart.categories.exists())
        self.assertFalse(second.categories.exists())


class AdminProvisioningCommandTests(TestCase):
    command_environment = {
        "ADMIN_USERNAME": "railway-admin",
        "ADMIN_EMAIL": "railway-admin@example.com",
        "ADMIN_PASSWORD": "A-strong-test-password-123!",
    }

    def run_provision_command(self, environment):
        output = StringIO()
        with patch.dict(os.environ, environment, clear=True):
            call_command("provision_admin", stdout=output)
        return output.getvalue()

    def test_missing_environment_variables_does_not_create_user(self):
        output = self.run_provision_command({})

        self.assertFalse(get_user_model().objects.exists())
        self.assertIn("provisioning skipped", output)

    def test_provisioning_requires_all_environment_variables(self):
        output = self.run_provision_command(
            {
                "ADMIN_USERNAME": self.command_environment["ADMIN_USERNAME"],
                "ADMIN_EMAIL": self.command_environment["ADMIN_EMAIL"],
            }
        )

        self.assertFalse(get_user_model().objects.exists())
        self.assertIn("ADMIN_PASSWORD", output)

    def test_provisioning_creates_superuser_without_logging_password(self):
        output = self.run_provision_command(self.command_environment)

        user = get_user_model().objects.get(username="railway-admin")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(self.command_environment["ADMIN_PASSWORD"]))
        self.assertNotIn(self.command_environment["ADMIN_PASSWORD"], output)

    def test_existing_user_is_not_modified_or_recreated(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username="railway-admin",
            email="original@example.com",
            password="Original-password-123!",
        )
        original_password_hash = user.password

        output = self.run_provision_command(self.command_environment)

        user.refresh_from_db()
        self.assertEqual(user.pk, user_model.objects.get(username="railway-admin").pk)
        self.assertEqual(user.email, "original@example.com")
        self.assertEqual(user.password, original_password_hash)
        self.assertFalse(user.is_staff)
        self.assertIn("already exists", output)


class AmazonPublishingWorkflowTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Mobiles", slug="mobiles")
        self.other_category = Category.objects.create(
            name="Accessories", slug="accessories", display_order=2
        )
        self.category.display_order = 1
        self.category.save(update_fields=["display_order"])
        self.amazon = AmazonProduct.objects.create(
            asin="B0WORKFLOW01",
            product_title="Workflow Phone",
            brand="Example",
            url="https://www.amazon.in/dp/B0WORKFLOW01",
            status=ImportStatus.COMPLETED,
            images=["https://images.example/workflow-phone.jpg"],
            current_selling_price_inr=29999,
            mrp_inr=34999,
        )

    def test_extracted_product_is_in_amazon_admin_queryset_and_pending(self):
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)

        self.assertIn(self.amazon, model_admin.get_queryset(RequestFactory().get("/admin/")))
        self.assertIn("https://images.example", str(model_admin.image_preview(self.amazon)))
        self.assertEqual(self.amazon.approval_status, "pending")
        self.assertFalse(self.amazon.published)

    def test_bulk_category_action_redirects_to_confirmation_page(self):
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = RequestFactory().post("/admin/importer/amazonproduct/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)

        response = model_admin.assign_categories(
            request,
            AmazonProduct.objects.filter(pk=self.amazon.pk),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("assign-categories/", response.url)
        self.assertIn(str(self.amazon.pk), response.url)

    def test_bulk_category_action_assigns_multiple_categories_to_multiple_products(self):
        second_amazon = AmazonProduct.objects.create(
            asin="B0WORKFLOW02",
            product_title="Workflow Laptop",
            url="https://www.amazon.in/dp/B0WORKFLOW02",
            status=ImportStatus.COMPLETED,
        )
        self.amazon.categories.add(self.category)
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = RequestFactory().post(
            "/admin/importer/amazonproduct/assign-categories/",
            {
                "ids": f"{self.amazon.pk},{second_amazon.pk}",
                "categories": [str(self.category.pk), str(self.other_category.pk)],
            },
        )
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)

        with patch.object(model_admin, "message_user"):
            response = model_admin.assign_categories_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(self.amazon.categories.values_list("pk", flat=True)),
            {self.category.pk, self.other_category.pk},
        )
        self.assertEqual(
            set(second_amazon.categories.values_list("pk", flat=True)),
            {self.category.pk, self.other_category.pk},
        )

    def test_bulk_category_action_rejects_empty_selection_without_mutation(self):
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = RequestFactory().post(
            "/admin/importer/amazonproduct/assign-categories/",
            {"ids": str(self.amazon.pk)},
        )
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)

        with patch.object(model_admin.admin_site, "each_context", return_value={}):
            response = model_admin.assign_categories_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertFalse(self.amazon.categories.exists())

    def test_bulk_category_assignment_preserves_existing_categories_and_is_publishable(self):
        assign_amazon_product_categories(
            [self.amazon.pk],
            [self.category],
        )
        assign_amazon_product_categories(
            [self.amazon.pk],
            [self.other_category],
        )
        approve_amazon_product(self.amazon)

        product = publish_amazon_product(self.amazon)

        self.assertEqual(
            set(self.amazon.categories.values_list("pk", flat=True)),
            {self.category.pk, self.other_category.pk},
        )
        self.assertEqual(product.category_id, self.category.pk)

    def test_pending_product_cannot_publish(self):
        with self.assertRaises(PublishValidationError):
            publish_amazon_product(self.amazon)
        self.assertFalse(Product.objects.exists())

    def test_approval_is_separate_from_publishing(self):
        approve_amazon_product(self.amazon)

        self.amazon.refresh_from_db()
        self.assertEqual(self.amazon.approval_status, "approved")
        self.assertFalse(self.amazon.published)
        self.assertFalse(Product.objects.exists())

    def test_publishing_requires_category(self):
        approve_amazon_product(self.amazon)

        with self.assertRaises(PublishValidationError):
            publish_amazon_product(self.amazon)
        self.assertFalse(Product.objects.exists())

    def test_publishing_with_categories_creates_public_product_and_price(self):
        self.amazon.categories.set([self.category, self.other_category])
        approve_amazon_product(self.amazon)

        product = publish_amazon_product(self.amazon)

        self.amazon.refresh_from_db()
        self.assertTrue(self.amazon.published)
        self.assertEqual(self.amazon.published_product, product)
        self.assertEqual(product.category, self.category)
        self.assertTrue(Product.objects.public().filter(pk=product.pk).exists())
        self.assertEqual(
            ProductPrice.objects.get(product=product, platform=ProductPrice.Platform.AMAZON).price,
            29999,
        )

    def test_unpublishing_removes_product_from_public_listing(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        product = publish_amazon_product(self.amazon)

        unpublish_amazon_product(self.amazon)

        self.assertFalse(Product.objects.public().filter(pk=product.pk).exists())
        self.assertNotContains(self.client.get("/products/"), "Workflow Phone")

    def test_amazon_image_url_is_preferred_and_uploaded_image_is_fallback(self):
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)
        product = publish_amazon_product(self.amazon)
        from apps.products.services import build_product_card

        product = Product.objects.prefetch_related("amazon_products").get(pk=product.pk)
        product.list_prices = list(product.prices.all())
        card = build_product_card(product, price_attribute="list_prices")
        self.assertEqual(card["image_url"], "https://images.example/workflow-phone.jpg")

        self.amazon.images = []
        self.amazon.save(update_fields=["images", "updated_at"])
        product.marketplace_image_url = ""
        product.featured_image = "products/featured/fallback.jpg"
        product.save(update_fields=["marketplace_image_url", "featured_image", "updated_at"])
        product = Product.objects.prefetch_related("amazon_products").get(pk=product.pk)
        product.list_prices = list(product.prices.all())
        card = build_product_card(product, price_attribute="list_prices")
        self.assertTrue(card["image_url"].endswith("/media/products/featured/fallback.jpg"))

    def test_publishing_transfers_first_marketplace_image_url_to_product(self):
        self.amazon.images = ["", "https://images.example/primary.jpg"]
        self.amazon.save(update_fields=["images", "updated_at"])
        self.amazon.categories.set([self.category])
        approve_amazon_product(self.amazon)

        product = publish_amazon_product(self.amazon)

        self.assertEqual(product.marketplace_image_url, "https://images.example/primary.jpg")
