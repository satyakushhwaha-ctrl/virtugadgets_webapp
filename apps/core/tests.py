from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.categories.models import Category
from apps.products.models import Product, ProductPrice
from apps.importer.models import (
    AmazonProduct,
    AmazonSearchResult,
    FlipkartSearchResult,
    ImporterJob,
    ImporterJobMarketplace,
    ImporterJobStatus,
    ImporterJobType,
    SearchKeyword,
)
from apps.importer.services.jobs import (
    create_job,
    enqueue_job,
    mark_job_completed,
    mark_job_failed,
    mark_job_partial,
    mark_job_queued,
    mark_job_running,
    mark_job_skipped,
    update_job_progress,
)
from apps.core.tasks import (
    amazon_product_extraction_task,
    amazon_search_task,
    flipkart_product_extraction_task,
    flipkart_product_search_task,
    flipkart_search_task,
)


@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class HomeViewTests(TestCase):
    def setUp(self) -> None:
        self.category = Category.objects.create(
            name="Mobiles",
            slug="mobiles",
            description="Smartphones and mobile accessories.",
            display_order=20,
        )
        self.second_category = Category.objects.create(
            name="Accessories",
            slug="accessories",
            description="Useful tech accessories and wearables.",
            display_order=10,
        )
        self.inactive_category = Category.objects.create(
            name="Hidden",
            slug="hidden",
            is_active=False,
        )

        for index in range(9):
            product = Product.objects.create(
                category=self.category,
                title=f"Product {index}",
                slug=f"product-{index}",
                brand="Samsung",
                is_active=True,
            )
            ProductPrice.objects.create(
                product=product,
                platform=ProductPrice.Platform.AMAZON,
                price=Decimal("1000.00") + index,
                mrp=Decimal("1200.00"),
                discount_percent=17,
                affiliate_url="https://www.amazon.in/",
            )
            ProductPrice.objects.create(
                product=product,
                platform=ProductPrice.Platform.FLIPKART,
                price=Decimal("900.00") + index,
                mrp=Decimal("1200.00"),
                discount_percent=25,
                affiliate_url="https://www.flipkart.com/",
            )

        Product.objects.create(
            category=self.category,
            title="Inactive Product",
            slug="inactive-product",
            brand="Apple",
            is_active=False,
        )
        Product.objects.create(
            category=self.inactive_category,
            title="Inactive Category Product",
            slug="inactive-category-product",
            brand="Apple",
        )

    def test_homepage_passes_active_categories_and_latest_products(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compare Prices Before You Buy.")
        self.assertContains(response, "Mobiles")
        self.assertContains(response, "/products/?category=mobiles")
        self.assertContains(response, "/products/?category=accessories")
        self.assertNotContains(response, "Hidden")
        self.assertNotContains(response, "/products/?category=hidden")
        self.assertNotContains(response, "Inactive Category Product")
        self.assertContains(response, "View Details", count=8)
        self.assertEqual(len(response.context["products"]), 8)

    def test_homepage_avoids_n_plus_one_queries(self) -> None:
        with self.assertNumQueries(6):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)


class ScrapingCeleryTaskTests(TestCase):
    def setUp(self):
        self.keyword = SearchKeyword.objects.create(keyword="HP Victus")
        self.amazon = AmazonProduct.objects.create(
            asin="B0CELERY0001",
            product_title="HP Victus Ryzen 7 7445HS",
            url="https://www.amazon.in/dp/B0CELERY0001",
        )

    @patch("apps.importer.services.amazon_search_results.run_amazon_search_for_keyword")
    def test_amazon_search_task_loads_id_and_reuses_service(self, service):
        service.return_value = SimpleNamespace(results_found=2, saved=2)
        result = amazon_search_task.run(str(self.keyword.pk))
        service.assert_called_once_with(SearchKeyword.objects.get(pk=self.keyword.pk))
        self.assertEqual(result["status"], "completed")


class ImporterJobTests(TestCase):
    def setUp(self):
        self.keyword = SearchKeyword.objects.create(keyword="HP Victus")
        self.amazon = AmazonProduct.objects.create(
            asin="B0JOB000001",
            product_title="HP Victus Ryzen 7 7445HS",
            url="https://www.amazon.in/dp/B0JOB000001",
        )

    def test_job_lifecycle_and_progress(self):
        job = create_job(
            title="Amazon Search — iphone 16",
            job_type=ImporterJobType.AMAZON_SEARCH,
            marketplace=ImporterJobMarketplace.AMAZON,
            total_items=20,
        )
        self.assertEqual(job.status, ImporterJobStatus.PENDING)
        mark_job_queued(job, "celery-123")
        self.assertEqual(job.status, ImporterJobStatus.QUEUED)
        self.assertEqual(job.celery_task_id, "celery-123")
        mark_job_running(job)
        update_job_progress(job, processed_items=12, success_count=10, failed_count=1, skipped_count=1)
        self.assertEqual(job.progress_percent, 60)
        self.assertEqual(job.progress_display, "12 / 20 (60%)")
        mark_job_partial(job, "10 succeeded; 1 failed; 1 skipped.")
        job.refresh_from_db()
        self.assertEqual(job.status, ImporterJobStatus.PARTIAL)
        self.assertIsNotNone(job.completed_at)

    def test_job_failure_and_skipped_lifecycle_keep_reason(self):
        failed = create_job(title="Failed", job_type=ImporterJobType.FLIPKART_SEARCH)
        mark_job_running(failed)
        mark_job_failed(failed, TimeoutError("Flipkart search timed out after 90 seconds."))
        skipped = create_job(title="Skipped", job_type=ImporterJobType.BEST_MATCH_FLIPKART)
        mark_job_running(skipped)
        mark_job_skipped(skipped, "No sufficiently matched Flipkart candidate.")
        failed.refresh_from_db()
        skipped.refresh_from_db()
        self.assertEqual(failed.status, ImporterJobStatus.FAILED)
        self.assertIn("timed out", failed.error_message)
        self.assertEqual(skipped.status, ImporterJobStatus.SKIPPED)
        self.assertIn("No sufficiently", skipped.result_message)

    def test_enqueue_saves_celery_id_and_queues_job(self):
        class FakeTask:
            def delay(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                return SimpleNamespace(id="celery-job-123")

        job = create_job(title="Amazon Search — test", job_type=ImporterJobType.AMAZON_SEARCH)
        queued = enqueue_job(task=FakeTask(), job=job, args=("keyword-id",))
        self.assertEqual(queued.status, ImporterJobStatus.QUEUED)
        self.assertEqual(queued.celery_task_id, "celery-job-123")
        self.assertEqual(queued.queued_at is not None, True)

    def test_job_admin_exposes_tracking_fields(self):
        from apps.importer.admin import ImporterJobAdmin
        from django.contrib import admin

        model_admin = ImporterJobAdmin(ImporterJob, admin.site)
        self.assertIn("progress", model_admin.list_display)
        self.assertIn("celery_task_id", model_admin.search_fields)
        self.assertIn("celery_task_id", model_admin.readonly_fields)

    @patch("apps.importer.services.amazon_product.process_amazon_search_result", return_value=True)
    def test_amazon_extraction_task_loads_id_and_reuses_service(self, service):
        source = AmazonSearchResult.objects.create(
            keyword=self.keyword, asin="B0CELERY0002", title="Laptop",
            product_url="https://www.amazon.in/dp/B0CELERY0002", position=1,
        )
        AmazonProduct.objects.create(
            asin=source.asin, url=source.product_url,
        )
        result = amazon_product_extraction_task.run(str(source.pk))
        self.assertEqual(service.call_args.args[0], AmazonSearchResult.objects.get(pk=source.pk))
        self.assertIn("on_basic_data", service.call_args.kwargs)
        self.assertEqual(result["status"], "completed")
        job = ImporterJob.objects.get(pk=result["job_id"])
        self.assertEqual(job.status, ImporterJobStatus.COMPLETED)
        self.assertEqual(job.celery_task_id, "")
        self.assertEqual(job.amazon_product.asin, source.asin)

    @patch("apps.importer.services.amazon_product.process_amazon_search_result", side_effect=RuntimeError("Amazon detail failed"))
    def test_amazon_extraction_task_records_failure_reason(self, service):
        source = AmazonSearchResult.objects.create(
            keyword=self.keyword, asin="B0CELERYFAIL1", title="Laptop",
            product_url="https://www.amazon.in/dp/B0CELERYFAIL1", position=1,
        )
        with self.assertRaises(RuntimeError):
            amazon_product_extraction_task.run(str(source.pk))
        job = ImporterJob.objects.filter(title__contains=source.asin).get()
        self.assertEqual(job.status, ImporterJobStatus.FAILED)
        self.assertIn("Amazon detail failed", job.error_message)

    @patch("apps.importer.services.flipkart_search_results.run_flipkart_search_for_keyword")
    def test_flipkart_search_task_loads_id_and_reuses_service(self, service):
        service.return_value = SimpleNamespace(failed=False, candidates_found=1, saved=1)
        result = flipkart_search_task.run(str(self.keyword.pk))
        service.assert_called_once_with(SearchKeyword.objects.get(pk=self.keyword.pk))
        self.assertEqual(result["status"], "completed")

    @patch("apps.importer.services.flipkart_product.process_flipkart_search_result", return_value=True)
    def test_flipkart_extraction_task_loads_id_and_reuses_service(self, service):
        source = FlipkartSearchResult.objects.create(
            amazon_product=self.amazon, pid="MOBCELERY0001", title="HP Victus",
            product_url="https://www.flipkart.com/p?pid=MOBCELERY0001", position=1,
        )
        from apps.importer.models import FlipkartProduct
        FlipkartProduct.objects.create(
            search_result=source, pid=source.pid, url=source.product_url,
        )
        result = flipkart_product_extraction_task.run(str(source.pk))
        service.assert_called_once_with(FlipkartSearchResult.objects.get(pk=source.pk))
        self.assertEqual(result["status"], "completed")
        job = ImporterJob.objects.get(pk=result["job_id"])
        self.assertEqual(job.status, ImporterJobStatus.COMPLETED)
        self.assertEqual(job.flipkart_product.pid, source.pid)

    @patch("apps.importer.services.flipkart_product.process_flipkart_search_result", side_effect=RuntimeError("Flipkart detail failed"))
    def test_flipkart_extraction_task_records_failure_reason(self, service):
        source = FlipkartSearchResult.objects.create(
            amazon_product=self.amazon, pid="MOBCELERYFAIL1", title="HP Victus",
            product_url="https://www.flipkart.com/p?pid=MOBCELERYFAIL1", position=1,
        )
        with self.assertRaises(RuntimeError):
            flipkart_product_extraction_task.run(str(source.pk))
        job = ImporterJob.objects.filter(title__contains=source.pid).get()
        self.assertEqual(job.status, ImporterJobStatus.FAILED)
        self.assertIn("Flipkart detail failed", job.error_message)

    @patch("apps.importer.services.flipkart_search_results.search_and_save_flipkart_candidates")
    def test_product_flipkart_search_task_accepts_product_id(self, service):
        service.return_value = SimpleNamespace(candidates_found=2, saved=2)
        result = flipkart_product_search_task.run(str(self.amazon.pk))
        service.assert_called_once_with(AmazonProduct.objects.get(pk=self.amazon.pk))
        self.assertEqual(result["status"], "completed")
