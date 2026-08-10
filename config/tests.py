from django.test import SimpleTestCase

from config.settings import _unique


class ProductionHostConfigurationTests(SimpleTestCase):
    def test_unique_removes_empty_and_duplicate_hosts(self):
        self.assertEqual(
            _unique(["virtugadgets.in", "", "virtugadgets.in", "www.virtugadgets.in"]),
            ["virtugadgets.in", "www.virtugadgets.in"],
        )
