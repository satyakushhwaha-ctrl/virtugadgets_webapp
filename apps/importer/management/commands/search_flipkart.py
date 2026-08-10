from django.core.management.base import BaseCommand, CommandError

from apps.importer.models import AmazonProduct, AmazonSearchResult, ImportStatus
from apps.importer.services.flipkart_search_results import (
    search_and_save_flipkart_candidates,
)


class Command(BaseCommand):
    help = "Find Flipkart candidate products for completed Amazon products."

    def add_arguments(self, parser):
        parser.add_argument("keyword", nargs="?", type=str)
        parser.add_argument("--asin", type=str)
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        keyword = (options.get("keyword") or "").strip()
        asin = (options.get("asin") or "").strip().upper()
        limit = options["limit"]
        if bool(keyword) == bool(asin):
            raise CommandError("Provide exactly one of keyword or --asin.")
        if limit <= 0:
            raise CommandError("--limit must be a positive integer.")

        products = self._find_products(keyword=keyword, asin=asin)
        products = products[:limit]

        self.stdout.write("Starting Flipkart search...\n")
        self.stdout.write(f"Products queued: {len(products)}\n")
        successful = 0
        failed = 0
        total_candidates_found = 0
        total_candidates_selected = 0
        total_saved = 0
        total_duplicates = 0

        for amazon_product in products:
            self.stdout.write(
                f"Source Amazon product:\n{amazon_product.product_title}"
            )
            self.stdout.write("Searching Flipkart...")
            try:
                summary = search_and_save_flipkart_candidates(amazon_product)
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"Failed: {exc}"))
                continue

            successful += 1
            candidates_selected = getattr(
                summary,
                "candidates_selected",
                summary.candidates_found,
            )
            total_candidates_found += summary.candidates_found
            total_candidates_selected += candidates_selected
            total_saved += summary.saved
            total_duplicates += summary.skipped_duplicates
            attempts = getattr(summary, "attempts", ())
            if attempts:
                for index, attempt in enumerate(attempts, start=1):
                    self.stdout.write(f"Search attempt {index}:")
                    self.stdout.write(attempt.query)
                    self.stdout.write(
                        f"Flipkart candidates found: {attempt.candidates_found}"
                    )
            else:
                self.stdout.write(f"Search query:\n{summary.query}")
                self.stdout.write(
                    f"Flipkart candidates found: {summary.candidates_found}"
                )
            self.stdout.write(f"Candidates selected: {candidates_selected}")
            self.stdout.write(f"New Flipkart candidates saved: {summary.saved}")
            self.stdout.write(
                "Existing Flipkart candidates skipped: "
                f"{summary.skipped_duplicates}\n"
            )

        self.stdout.write("Flipkart search completed.")
        self.stdout.write(f"Successful: {successful}")
        self.stdout.write(f"Failed: {failed}")
        self.stdout.write(f"Flipkart candidates found: {total_candidates_found}")
        self.stdout.write(f"Candidates selected: {total_candidates_selected}")
        self.stdout.write(f"New Flipkart candidates saved: {total_saved}")
        self.stdout.write(
            "Existing Flipkart candidates skipped: "
            f"{total_duplicates}"
        )

    def _find_products(self, keyword: str, asin: str):
        if asin:
            products = AmazonProduct.objects.filter(
                asin=asin,
                status=ImportStatus.COMPLETED,
            )
            if not products.exists():
                raise CommandError(f"Completed Amazon product not found: {asin}")
            return products

        search_result_asins = AmazonSearchResult.objects.filter(
            keyword__keyword=keyword,
        ).values("asin")
        products = AmazonProduct.objects.filter(
            asin__in=search_result_asins,
            status=ImportStatus.COMPLETED,
        ).distinct()
        if not products.exists():
            raise CommandError(
                f"No completed Amazon products found for keyword: {keyword}"
            )
        return products.order_by("asin")
