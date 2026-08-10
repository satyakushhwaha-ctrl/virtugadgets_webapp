from django.core.management.base import BaseCommand, CommandError

from apps.importer.models import AmazonSearchResult, SearchKeyword
from apps.importer.services.amazon_product import process_amazon_search_result


class Command(BaseCommand):
    help = "Extract Amazon product details into the staging table."

    def add_arguments(self, parser):
        parser.add_argument("keyword", type=str)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, **options):
        keyword = options["keyword"].strip()
        limit = options["limit"]
        if not keyword:
            raise CommandError("Keyword cannot be empty.")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be a positive integer.")

        try:
            search_keyword = SearchKeyword.objects.get(keyword=keyword)
        except SearchKeyword.DoesNotExist as exc:
            raise CommandError(f"Search keyword not found: {keyword}") from exc

        results = AmazonSearchResult.objects.filter(
            keyword=search_keyword,
            processed=False,
        ).order_by("position")
        if limit is not None:
            results = results[:limit]

        self.stdout.write("Starting Amazon product extraction...\n")
        self.stdout.write(f"Keyword: {keyword}")
        self.stdout.write(f"Products queued: {len(results)}\n")

        successful = 0
        failed = 0
        skipped = 0
        for index, search_result in enumerate(results, start=1):
            self.stdout.write(
                f"[{index}/{len(results)}] {search_result.asin}"
            )
            try:
                processed = process_amazon_search_result(search_result)
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"Failed: {exc}"))
                continue

            if processed:
                successful += 1
                self.stdout.write(self.style.SUCCESS("Completed"))
            else:
                skipped += 1
                self.stdout.write("Skipped")

        self.stdout.write("\nExtraction completed.")
        self.stdout.write(f"Successful: {successful}")
        self.stdout.write(f"Failed: {failed}")
        self.stdout.write(f"Skipped: {skipped}")
