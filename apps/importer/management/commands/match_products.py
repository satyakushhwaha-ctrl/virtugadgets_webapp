from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.importer.models import (
    AmazonProduct,
    FlipkartProduct,
    ImportStatus,
    MatchStatus,
    ProductMatch,
)
from apps.importer.services.product_matching import match_products


class Command(BaseCommand):
    help = "Compare staged Amazon and Flipkart products deterministically."

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
                f"Completed AmazonProduct not found: {asin}"
            ) from exc

        flipkart_products = FlipkartProduct.objects.filter(
            search_result__amazon_product=amazon_product,
            status=ImportStatus.COMPLETED,
        ).select_related("search_result")[:limit]

        self.stdout.write("Starting product matching...\n")
        self.stdout.write(f"Amazon:\n{amazon_product.product_title}")
        self.stdout.write(f"\nFlipkart candidates: {len(flipkart_products)}\n")

        matched = 0
        review = 0
        rejected = 0
        evaluated = []
        with transaction.atomic():
            for index, flipkart_product in enumerate(flipkart_products, start=1):
                result = match_products(amazon_product, flipkart_product)
                ProductMatch.objects.update_or_create(
                    amazon_product=amazon_product,
                    flipkart_product=flipkart_product,
                    defaults=result,
                )
                evaluated.append((flipkart_product, result))
                self.stdout.write(f"[{index}/{len(flipkart_products)}]")
                self.stdout.write(f"PID: {flipkart_product.pid}")
                self.stdout.write(f"Score: {result['score']}")
                self.stdout.write(
                    f"Confidence: {result['confidence'].upper()}"
                )
                self.stdout.write(
                    f"Status: {result['match_status'].upper()}\n"
                )
                if result["match_status"] == MatchStatus.MATCHED:
                    matched += 1
                elif result["match_status"] == MatchStatus.REVIEW:
                    review += 1
                else:
                    rejected += 1

        self.stdout.write("Matching completed.")
        self.stdout.write(f"Matched: {matched}")
        self.stdout.write(f"Review: {review}")
        self.stdout.write(f"Rejected: {rejected}")

        if evaluated:
            best_product, best_result = max(
                evaluated,
                key=lambda item: item[1]["score"],
            )
            self.stdout.write("\nBest candidate:")
            self.stdout.write(f"PID: {best_product.pid}")
            self.stdout.write(f"Score: {best_result['score']}")
            self.stdout.write(
                f"Confidence: {best_result['confidence'].upper()}"
            )
