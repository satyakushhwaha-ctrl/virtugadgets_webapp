from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory

from .admin import AmazonProductAdmin, FlipkartProductAdmin
from .models import (
    AmazonProduct,
    AmazonSearchResult,
    FlipkartProduct,
    FlipkartSearchResult,
    ImporterJob,
    ImporterJobStatus,
    SearchKeyword,
)
from .services.refresh import (
    queue_amazon_product_refresh,
    queue_flipkart_product_refresh,
)


class RefreshActionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="refresh-admin", password="password"
        )
        self.task = MagicMock()
        self.task.delay.return_value.id = "celery-refresh-1"
        keyword = SearchKeyword.objects.create(keyword="refresh-test")
        self.amazon_result = AmazonSearchResult.objects.create(
            keyword=keyword,
            asin="B0REFRESH01",
            title="Refresh phone",
            product_url="https://www.amazon.in/dp/B0REFRESH01",
            position=1,
        )
        self.amazon = AmazonProduct.objects.create(
            asin="B0REFRESH01",
            url=self.amazon_result.product_url,
            product_title="Refresh phone",
        )
        self.flipkart_result = FlipkartSearchResult.objects.create(
            pid="MOBREFRESH01",
            title="Refresh phone",
            product_url="https://www.flipkart.com/phone/p?pid=MOBREFRESH01",
            position=1,
        )
        self.flipkart = FlipkartProduct.objects.create(
            search_result=self.flipkart_result,
            pid="MOBREFRESH01",
            url=self.flipkart_result.product_url,
            product_title="Refresh phone",
        )

    def test_amazon_refresh_queues_existing_task_and_job(self):
        job, message = queue_amazon_product_refresh(
            product=self.amazon, task=self.task, user=self.user
        )
        self.assertIsNone(message)
        self.assertEqual(job.status, ImporterJobStatus.QUEUED)
        self.task.delay.assert_called_once_with(str(self.amazon_result.pk), job_id=str(job.pk))
        self.assertEqual(ImporterJob.objects.filter(amazon_product=self.amazon).count(), 1)

    def test_flipkart_refresh_queues_existing_task_and_job(self):
        job, message = queue_flipkart_product_refresh(
            product=self.flipkart, task=self.task, user=self.user
        )
        self.assertIsNone(message)
        self.assertEqual(job.status, ImporterJobStatus.QUEUED)
        self.task.delay.assert_called_once_with(str(self.flipkart_result.pk), job_id=str(job.pk))

    def test_duplicate_amazon_refresh_is_skipped(self):
        first, _ = queue_amazon_product_refresh(product=self.amazon, task=self.task)
        second, message = queue_amazon_product_refresh(product=self.amazon, task=self.task)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(message, "Amazon product extraction is already running.")
        self.assertEqual(self.task.delay.call_count, 1)

    def test_duplicate_flipkart_refresh_is_skipped(self):
        first, _ = queue_flipkart_product_refresh(product=self.flipkart, task=self.task)
        second, message = queue_flipkart_product_refresh(product=self.flipkart, task=self.task)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(message, "Flipkart product extraction is already running.")
        self.assertEqual(self.task.delay.call_count, 1)

    def test_completed_and_failed_products_can_refresh(self):
        for status in ("completed", "failed"):
            self.amazon.status = status
            self.amazon.save(update_fields=["status", "updated_at"])
            job, message = queue_amazon_product_refresh(product=self.amazon, task=self.task)
            self.assertIsNone(message)
            job.status = ImporterJobStatus.COMPLETED
            job.save(update_fields=["status", "updated_at"])

    @patch("apps.importer.admin.queue_amazon_product_refresh")
    def test_amazon_detail_action_queues_without_synchronous_extraction(self, queue):
        queue.return_value = (MagicMock(), None)
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = RequestFactory().post("/admin/", {"_refresh_product": "Refresh"})
        request.user = self.user
        with patch.object(model_admin, "message_user") as message:
            with patch("apps.importer.admin.process_amazon_search_result") as extract:
                model_admin.response_change(request, self.amazon)
        queue.assert_called_once()
        extract.assert_not_called()
        self.assertIn("queued", str(message.call_args_list).lower())

    @patch("apps.importer.admin.queue_flipkart_product_refresh")
    def test_flipkart_detail_action_queues_without_synchronous_extraction(self, queue):
        queue.return_value = (MagicMock(), None)
        model_admin = FlipkartProductAdmin(FlipkartProduct, admin.site)
        request = RequestFactory().post("/admin/", {"_refresh_product": "Refresh"})
        request.user = self.user
        with patch.object(model_admin, "message_user") as message:
            with patch("apps.importer.admin.process_flipkart_search_result") as extract:
                model_admin.response_change(request, self.flipkart)
        queue.assert_called_once()
        extract.assert_not_called()
        self.assertIn("queued", str(message.call_args_list).lower())

    @patch("apps.importer.admin.queue_amazon_product_refresh")
    def test_amazon_bulk_refresh_reports_queued_and_skipped(self, queue):
        queue.side_effect = [(MagicMock(), None), (MagicMock(), "Amazon product extraction is already running.")]
        second = AmazonProduct.objects.create(asin="B0REFRESH02", url="https://www.amazon.in/dp/B0REFRESH02")
        model_admin = AmazonProductAdmin(AmazonProduct, admin.site)
        request = RequestFactory().post("/admin/")
        request.user = self.user
        with patch.object(model_admin, "message_user") as message:
            model_admin.refresh_selected(request, AmazonProduct.objects.filter(pk__in=[self.amazon.pk, second.pk]))
        self.assertIn("1 Amazon product(s) queued", str(message.call_args_list))
        self.assertIn("1 skipped", str(message.call_args_list))

    @patch("apps.importer.admin.queue_flipkart_product_refresh")
    def test_flipkart_bulk_refresh_queues_selected_products(self, queue):
        queue.return_value = (MagicMock(), None)
        model_admin = FlipkartProductAdmin(FlipkartProduct, admin.site)
        request = RequestFactory().post("/admin/")
        request.user = self.user
        with patch.object(model_admin, "message_user") as message:
            model_admin.refresh_selected(request, FlipkartProduct.objects.filter(pk=self.flipkart.pk))
        queue.assert_called_once()
        self.assertIn("1 Flipkart product(s) queued", str(message.call_args_list))
