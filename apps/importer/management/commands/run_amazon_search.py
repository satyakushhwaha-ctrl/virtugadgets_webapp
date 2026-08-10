from django.core.management.base import BaseCommand, CommandError

from apps.importer.models import SearchKeyword
from apps.importer.services.amazon_search_results import (
    run_amazon_search_for_keyword,
)


class Command(BaseCommand):
    help = "Search Amazon for a keyword and store the results."

    def add_arguments(self, parser):
        parser.add_argument("keyword", type=str)

    def handle(self, *args, **options):
        keyword = options["keyword"].strip()
        if not keyword:
            raise CommandError("Keyword cannot be empty.")

        search_keyword, _ = SearchKeyword.objects.get_or_create(keyword=keyword)
        self.stdout.write("Starting Amazon search...")
        self.stdout.write(f"Keyword: {keyword}")
        try:
            summary = run_amazon_search_for_keyword(search_keyword)
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Amazon search failed: {exc}"))
            raise CommandError("Amazon search failed.") from exc

        self.stdout.write(f"Results found: {summary.results_found}")
        self.stdout.write(f"Saved: {summary.saved}")
        self.stdout.write(f"Skipped duplicates: {summary.skipped_duplicates}")
        self.stdout.write(self.style.SUCCESS("Amazon search completed."))
