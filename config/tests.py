from django.test import SimpleTestCase

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
