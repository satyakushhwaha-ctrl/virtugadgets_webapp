from django.conf import settings
from django.test import SimpleTestCase

from apps.core.tasks import celery_health_check
from config.celery import app as celery_app
from config.settings import _hostname, _https_origin, _unique


class ProductionHostConfigurationTests(SimpleTestCase):
    def test_unique_removes_empty_and_duplicate_hosts(self):
        self.assertEqual(
            _unique(["virtugadgets.in", "", "virtugadgets.in", "www.virtugadgets.in"]),
            ["virtugadgets.in", "www.virtugadgets.in"],
        )

    def test_hostname_removes_scheme_and_path(self):
        self.assertEqual(_hostname("https://www.virtugadgets.in/admin/"), "www.virtugadgets.in")

    def test_https_origin_always_has_https_scheme(self):
        self.assertEqual(_https_origin("virtugadgets.in"), "https://virtugadgets.in")
        self.assertEqual(_https_origin("https://www.virtugadgets.in/"), "https://www.virtugadgets.in")


class CeleryConfigurationTests(SimpleTestCase):
    def test_celery_app_imports_with_config_settings(self):
        self.assertEqual(celery_app.main, "config")
        self.assertEqual(celery_app.conf.broker_url, settings.CELERY_BROKER_URL)

    def test_health_check_task_is_registered(self):
        self.assertIn(celery_health_check.name, celery_app.tasks)

    def test_health_check_task_executes_without_a_broker(self):
        self.assertEqual(celery_health_check.run(), "ok")

    def test_redis_configuration_uses_a_redis_url(self):
        self.assertTrue(settings.REDIS_URL.startswith("redis://"))
        self.assertEqual(settings.CELERY_RESULT_BACKEND, settings.REDIS_URL)
