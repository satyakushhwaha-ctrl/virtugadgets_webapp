from django.core.management.base import BaseCommand, CommandError

from apps.importer.models import (
    AmazonProduct,
    FlipkartSearchResult,
    ImportStatus,
)
from apps.importer.services.flipkart_product import (
    process_flipkart_search_result,
)


class Command(BaseCommand):
    help = "Extract selected Flipkart candidates into staging."

    def add_arguments(self, parser):
        parser.add_argument("--asin", required=True, type=str)
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        asin = options["asin"].strip().upper()
        limit = options["limit"]
        if not asin:
            raise CommandError("--asin cannot be empty.")
        if limit <= 0:
            raise CommandError("--limit must be a positive integer.")

        try:
            amazon_product = AmazonProduct.objects.get(
                asin=asin,
                status=ImportStatus.COMPLETED,
            )
        except AmazonProduct.DoesNotExist as exc:
            raise CommandError(
                f"Completed source AmazonProduct not found: {asin}"
            ) from exc

        results = FlipkartSearchResult.objects.filter(
            amazon_product=amazon_product,
            processed=False,
        ).order_by("position")[:limit]

        self.stdout.write("Starting Flipkart product extraction...\n")
        self.stdout.write("Source Amazon product:")
        self.stdout.write(amazon_product.product_title)
        self.stdout.write(f"\nFlipkart candidates queued: {len(results)}\n")

        successful = 0
        failed = 0
        skipped = 0
        for index, search_result in enumerate(results, start=1):
            self.stdout.write(f"[{index}/{len(results)}]")
            self.stdout.write(f"Flipkart PID:\n{search_result.pid}")
            self.stdout.write(f"Flipkart URL:\n{search_result.product_url}")
            self.stdout.write("Extracting Flipkart product details...")
            try:
                processed = process_flipkart_search_result(search_result)
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"Failed: {exc}"))
                continue

            if processed:
                successful += 1
                self.stdout.write(self.style.SUCCESS("Completed\n"))
            else:
                skipped += 1
                self.stdout.write("Skipped\n")

        self.stdout.write("Extraction completed.")
        self.stdout.write(f"Successful: {successful}")
        self.stdout.write(f"Failed: {failed}")
        self.stdout.write(f"Skipped: {skipped}")
