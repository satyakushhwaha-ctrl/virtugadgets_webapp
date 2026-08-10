from django.contrib import admin, messages
from django import forms
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html

from apps.categories.models import Category

from .models import (
    AmazonProduct,
    AmazonSearchResult,
    BatchStatus,
    FlipkartProduct,
    FlipkartSearchResult,
    ImportBatch,
    ProductMatch,
    SearchKeyword,
    ImportStatus,
)
from .services.amazon_search_results import run_amazon_search_for_keyword
from .services.amazon_product import process_amazon_search_result
from .services.flipkart_product import process_flipkart_search_result
from .services.flipkart_search_results import search_and_save_flipkart_candidates
from .services.product_matching import extract_model_identity, match_products
from .services.product_matching import run_product_matching_for_keyword
from .services.product_publisher import publish_product_match
from .services.batch_runner import cancel_batch, publish_approved_products, run_batch


class FlipkartSearchResultInline(admin.TabularInline):
    model = FlipkartSearchResult
    extra = 0
    fields = ("pid", "title", "product_url", "position", "processed")
    readonly_fields = fields
    show_change_link = True


class ProductMatchInline(admin.TabularInline):
    model = ProductMatch
    fk_name = "amazon_product"
    extra = 0
    fields = ("flipkart_product", "score", "confidence", "match_status")
    readonly_fields = fields
    show_change_link = True


class ProductMatchAdminForm(forms.ModelForm):
    class Meta:
        model = ProductMatch
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "publish_category" in self.fields:
            self.fields["publish_category"].queryset = Category.objects.filter(
                is_active=True,
            ).order_by("display_order", "name")
        if "publish_categories" in self.fields:
            self.fields["publish_categories"].queryset = Category.objects.filter(
                is_active=True,
            ).order_by("display_order", "name")


class AssignPublishCategoriesForm(forms.Form):
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.filter(is_active=True).order_by("display_order", "name"),
        required=True,
        label="Publish categories",
    )


class PublishedProductFilter(admin.SimpleListFilter):
    title = "Published"
    parameter_name = "published"

    def lookups(self, request, model_admin):
        return (("yes", "Published"), ("no", "Not published"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(match_status="published")
        if self.value() == "no":
            return queryset.exclude(match_status="published")
        return queryset


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    change_form_template = "admin/importer/importbatch/change_form.html"
    list_display = (
        "short_id", "keyword", "status", "amazon_results_count",
        "amazon_products_count", "flipkart_results_count",
        "flipkart_products_count", "matches_count", "created_at", "updated_at",
    )
    search_fields = ("keyword__keyword", "id")
    list_filter = ("status", "keyword")
    ordering = ("-created_at",)
    readonly_fields = (
        "status", "amazon_results_count", "amazon_products_count",
        "flipkart_results_count", "flipkart_products_count", "matches_count",
        "successful_count", "failed_count", "error_message", "started_at",
        "completed_at", "created_at", "updated_at", "pipeline_progress",
        "review_matches_link",
    )
    fieldsets = (
        (None, {"fields": ("keyword",)}),
        ("Pipeline", {"fields": (
            "status", "pipeline_progress", "amazon_results_count",
            "amazon_products_count", "flipkart_results_count",
            "flipkart_products_count", "matches_count", "successful_count",
            "failed_count", "error_message", "review_matches_link",
        )}),
        ("Timestamps", {"fields": ("started_at", "completed_at", "created_at", "updated_at")}),
    )
    actions = (
        "run_batch_action", "resume_batch_action", "cancel_batch_action",
        "review_matches_action", "publish_approved_products_action",
    )

    @admin.display(description="Batch ID")
    def short_id(self, obj):
        return str(obj.pk)[:8]

    @admin.display(description="Pipeline progress")
    def pipeline_progress(self, obj):
        steps = (
            ("Amazon Search", obj.amazon_results_count),
            ("Amazon Extraction", obj.amazon_products_count),
            ("Flipkart Search", obj.flipkart_results_count),
            ("Flipkart Extraction", obj.flipkart_products_count),
            ("Product Matching", obj.matches_count),
        )
        lines = [f"{name}: {count}" for name, count in steps]
        lines.append(f"Status: {obj.get_status_display()}")
        return format_html("<pre>{}</pre>", "\n".join(lines))

    @admin.display(description="Review matches")
    def review_matches_link(self, obj):
        url = reverse("admin:importer_productmatch_changelist")
        return format_html('<a href="{}?batch={}">Review Matches</a>', url, obj.pk)

    def response_change(self, request, obj):
        if "_run_batch" in request.POST or "_resume_batch" in request.POST:
            try:
                run_batch(obj)
                self.message_user(request, "Batch completed and is ready for review.")
            except Exception as exc:
                self.message_user(request, f"Batch failed: {exc}", level=messages.ERROR)
            return HttpResponseRedirect(request.path)
        if "_cancel_batch" in request.POST:
            try:
                cancel_batch(obj)
                self.message_user(request, "Batch cancelled.")
            except Exception as exc:
                self.message_user(request, f"Batch could not be cancelled: {exc}", level=messages.ERROR)
            return HttpResponseRedirect(request.path)
        if "_review_matches" in request.POST:
            return HttpResponseRedirect(
                f"{reverse('admin:importer_productmatch_changelist')}?batch={obj.pk}"
            )
        if "_publish_approved" in request.POST:
            summary = publish_approved_products(obj, user=request.user)
            level = messages.ERROR if summary.failed else messages.SUCCESS
            self.message_user(
                request,
                f"Approved publishing completed. Successful: {summary.successful} "
                f"Failed: {summary.failed} Skipped: {summary.skipped}.",
                level=level,
            )
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)

    def _run_selected(self, request, queryset, resume=False):
        successful = failed = 0
        for batch in queryset:
            try:
                run_batch(batch)
                successful += 1
            except Exception as exc:
                failed += 1
                self.message_user(request, f"{batch}: {exc}", level=messages.ERROR)
        self.message_user(
            request,
            f"Batch {'resume' if resume else 'run'} completed. "
            f"Successful: {successful} Failed: {failed}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )

    @admin.action(description="Run Batch")
    def run_batch_action(self, request, queryset):
        self._run_selected(request, queryset)

    @admin.action(description="Resume Batch")
    def resume_batch_action(self, request, queryset):
        self._run_selected(request, queryset, resume=True)

    @admin.action(description="Cancel Batch")
    def cancel_batch_action(self, request, queryset):
        successful = failed = 0
        for batch in queryset:
            try:
                cancel_batch(batch)
                successful += 1
            except Exception as exc:
                failed += 1
                self.message_user(request, f"{batch}: {exc}", level=messages.ERROR)
        self.message_user(
            request,
            f"Batch cancellation completed. Successful: {successful} Failed: {failed}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )

    @admin.action(description="Review Matches")
    def review_matches_action(self, request, queryset):
        batch = queryset.first()
        if batch:
            return HttpResponseRedirect(
                f"{reverse('admin:importer_productmatch_changelist')}?batch={batch.pk}"
            )

    @admin.action(description="Publish Approved Products")
    def publish_approved_products_action(self, request, queryset):
        total = {"successful": 0, "failed": 0, "skipped": 0}
        for batch in queryset:
            summary = publish_approved_products(batch, user=request.user)
            for field in total:
                total[field] += getattr(summary, field)
        self.message_user(
            request,
            f"Approved publishing completed. Successful: {total['successful']} "
            f"Failed: {total['failed']} Skipped: {total['skipped']}.",
            level=messages.ERROR if total["failed"] else messages.SUCCESS,
        )


@admin.register(SearchKeyword)
class SearchKeywordAdmin(admin.ModelAdmin):
    list_display = (
        "keyword",
        "status",
        "total_results",
        "amazon_extraction_summary",
        "flipkart_search_summary",
        "product_matching_summary",
        "matching_phase_status",
        "created_at",
        "updated_at",
    )
    search_fields = ("keyword",)
    list_filter = ("status", "matching_status")
    ordering = ("-created_at",)
    actions = ("run_amazon_search", "run_product_matching")

    def _amazon_products(self, obj):
        return AmazonProduct.objects.filter(
            asin__in=obj.amazon_results.values("asin"),
        )

    @admin.display(description="Amazon extraction")
    def amazon_extraction_summary(self, obj):
        products = self._amazon_products(obj)
        count = products.filter(status="completed").count()
        total = products.count()
        if not total:
            return "0 products · Pending"
        status = "Completed" if count == total else "In progress"
        return f"{count}/{total} products · {status}"

    @admin.display(description="Flipkart search")
    def flipkart_search_summary(self, obj):
        candidates = FlipkartSearchResult.objects.filter(
            amazon_product__in=self._amazon_products(obj),
        )
        products_searched = candidates.values("amazon_product_id").distinct().count()
        candidate_count = candidates.count()
        total_products = self._amazon_products(obj).filter(status="completed").count()
        if not total_products:
            status = "Waiting for Amazon products"
        elif products_searched == total_products:
            status = "Completed"
        elif products_searched:
            status = "In progress"
        else:
            status = "Pending"
        return f"{products_searched}/{total_products} searched · {candidate_count} candidates · {status}"

    @admin.display(description="Product matching")
    def product_matching_summary(self, obj):
        matches = ProductMatch.objects.filter(
            amazon_product__in=self._amazon_products(obj),
        )
        return f"{matches.count()} matches"

    @admin.display(description="Matching status")
    def matching_phase_status(self, obj):
        return obj.get_matching_status_display()

    @admin.action(description="Run Product Matching")
    def run_product_matching(self, request, queryset):
        aggregate = {
            "amazon_products": 0,
            "flipkart_products": 0,
            "matches_created": 0,
            "matches_updated": 0,
            "high_confidence": 0,
            "medium_confidence": 0,
            "low_confidence": 0,
            "no_candidate": 0,
            "failed": 0,
        }
        failed_keywords = []
        for search_keyword in queryset.iterator():
            search_keyword.matching_status = ImportStatus.RUNNING
            search_keyword.save(update_fields=["matching_status", "updated_at"])
            try:
                summary = run_product_matching_for_keyword(search_keyword)
            except Exception as exc:
                search_keyword.matching_status = ImportStatus.FAILED
                search_keyword.save(update_fields=["matching_status", "updated_at"])
                failed_keywords.append(f"{search_keyword.keyword} ({exc})")
                continue
            search_keyword.matching_status = (
                ImportStatus.FAILED if summary.failed else ImportStatus.COMPLETED
            )
            search_keyword.save(update_fields=["matching_status", "updated_at"])
            for field in aggregate:
                aggregate[field] += getattr(summary, field)

        if failed_keywords:
            self.message_user(
                request,
                "Product matching failures: " + "; ".join(failed_keywords),
                level=messages.ERROR,
            )
        self.message_user(
            request,
            "Product matching completed. "
            f"Amazon products: {aggregate['amazon_products']} "
            f"Flipkart products: {aggregate['flipkart_products']} "
            f"Product matches created/updated: "
            f"{aggregate['matches_created'] + aggregate['matches_updated']} "
            f"(created {aggregate['matches_created']}, updated {aggregate['matches_updated']}) "
            f"High confidence: {aggregate['high_confidence']} "
            f"Medium confidence: {aggregate['medium_confidence']} "
            f"Low confidence: {aggregate['low_confidence']} "
            f"No candidate: {aggregate['no_candidate']} "
            f"Failed: {aggregate['failed']}.",
            level=messages.ERROR if aggregate["failed"] or failed_keywords else messages.SUCCESS,
        )

    @admin.action(description="Run Amazon Search")
    def run_amazon_search(self, request, queryset):
        completed = failed = 0
        for search_keyword in queryset:
            try:
                summary = run_amazon_search_for_keyword(search_keyword)
            except Exception as exc:
                failed += 1
                self.message_user(
                    request, f"{search_keyword.keyword}: Amazon search failed: {exc}",
                    level=messages.ERROR,
                )
                continue
            completed += 1
            self.message_user(
                request,
                f"{search_keyword.keyword}: found {summary.results_found}, "
                f"saved {summary.saved}, skipped {summary.skipped_duplicates} duplicates.",
            )
        if failed:
            self.message_user(
                request,
                f"Amazon search completed for {completed}; failed for {failed}.",
                level=messages.ERROR,
            )


@admin.register(AmazonSearchResult)
class AmazonSearchResultAdmin(admin.ModelAdmin):
    list_display = ("asin", "title", "keyword", "position", "sponsored", "processed", "created_at")
    search_fields = ("asin", "title", "keyword__keyword")
    list_filter = ("processed", "sponsored")
    ordering = ("position",)
    actions = ("extract_amazon_products",)

    @admin.action(description="Extract selected Amazon products")
    def extract_amazon_products(self, request, queryset):
        """Extract every selected result through the existing staging service."""
        selected = queryset.count()
        successful = failed = skipped = 0
        failed_asins = []

        for search_result in queryset.select_related("keyword").iterator():
            try:
                processed = process_amazon_search_result(search_result)
            except Exception as exc:
                failed += 1
                failed_asins.append(f"{search_result.asin} ({exc})")
                continue

            if processed:
                successful += 1
            else:
                skipped += 1

        if failed_asins:
            self.message_user(
                request,
                "Amazon extraction failures: " + "; ".join(failed_asins),
                level=messages.ERROR,
            )
        self.message_user(
            request,
            f"Amazon extraction completed. Selected: {selected} "
            f"Successful: {successful} Failed: {failed} Skipped: {skipped}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )


@admin.register(FlipkartSearchResult)
class FlipkartSearchResultAdmin(admin.ModelAdmin):
    list_display = ("amazon_product", "pid", "title", "product_url", "processed", "created_at")
    search_fields = ("pid", "title", "amazon_product__asin", "amazon_product__product_title")
    list_filter = ("processed", "sponsored")
    ordering = ("position", "-created_at")
    actions = ("extract_flipkart_products",)

    @admin.action(description="Extract Flipkart Products")
    def extract_flipkart_products(self, request, queryset):
        successful = failed = skipped = 0
        for search_result in queryset.select_related("amazon_product"):
            try:
                processed = process_flipkart_search_result(search_result)
            except Exception as exc:
                failed += 1
                self.message_user(
                    request,
                    f"{search_result.pid}: Flipkart extraction failed: {exc}",
                    level=messages.ERROR,
                )
                continue
            if processed:
                successful += 1
            else:
                skipped += 1
        self.message_user(
            request,
            f"Flipkart extraction completed. Selected: {queryset.count()} "
            f"Successful: {successful} Failed: {failed} Skipped: {skipped}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )


@admin.register(FlipkartProduct)
class FlipkartProductAdmin(admin.ModelAdmin):
    list_display = (
        "pid", "product_title", "brand", "current_selling_price_inr",
        "availability", "status", "extracted_at",
    )
    search_fields = (
        "pid", "product_title", "brand", "search_result__pid",
        "search_result__amazon_product__asin",
    )
    list_filter = ("status", "availability", "brand")
    ordering = ("-updated_at",)
    readonly_fields = ("source_search_result", "updated_at", "extracted_at")
    fieldsets = (
        (None, {"fields": ("pid", "search_result", "source_search_result", "product_title", "brand", "url", "availability", "status", "error_message")}),
        ("Pricing", {"fields": ("mrp_inr", "current_selling_price_inr", "selling_price_min_inr", "selling_price_max_inr", "discount_percentage")}),
        ("Specifications", {"fields": ("processor", "ram", "storage", "operating_system", "display_size", "resolution", "color", "weight_kg", "software", "warranty")}),
        ("Metadata", {"fields": ("images", "primary_seller", "seller_rating", "extracted_at", "updated_at")}),
    )

    @admin.display(description="Source search result")
    def source_search_result(self, obj):
        if not obj or not obj.search_result_id:
            return "-"
        url = reverse("admin:importer_flipkartsearchresult_change", args=[obj.search_result_id])
        return format_html('<a href="{}">{}</a>', url, obj.search_result.pid)


@admin.register(AmazonProduct)
class AmazonProductAdmin(admin.ModelAdmin):
    list_display = (
        "asin",
        "product_title",
        "search_keyword",
        "extraction_status",
        "flipkart_search_status",
        "flipkart_candidates",
        "updated_at",
    )
    search_fields = ("asin", "product_title", "brand")
    list_filter = ("status", "availability", "brand")
    ordering = ("-updated_at",)
    actions = ("search_flipkart",)
    inlines = (FlipkartSearchResultInline, ProductMatchInline)

    @admin.action(description="Search Flipkart for selected products")
    def search_flipkart(self, request, queryset):
        selected = queryset.count()
        successful = failed = skipped = 0
        candidates_found = duplicates_skipped = 0
        failed_asins = []
        for amazon_product in queryset.iterator():
            if amazon_product.status != "completed":
                skipped += 1
                continue
            try:
                summary = search_and_save_flipkart_candidates(amazon_product)
            except Exception as exc:
                failed += 1
                failed_asins.append(f"{amazon_product.asin} ({exc})")
                continue
            successful += 1
            candidates_found += summary.candidates_found
            duplicates_skipped += summary.skipped_duplicates
        if failed_asins:
            self.message_user(
                request,
                "Flipkart search failures: " + "; ".join(failed_asins),
                level=messages.ERROR,
            )
        self.message_user(
            request,
            "Flipkart search completed. "
            f"Amazon products processed: {selected} "
            f"Successful: {successful} Failed: {failed} Skipped: {skipped} "
            f"Candidates found: {candidates_found} "
            f"Duplicates skipped: {duplicates_skipped}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )

    @admin.display(description="Search keyword")
    def search_keyword(self, obj):
        keyword = SearchKeyword.objects.filter(
            amazon_results__asin=obj.asin,
        ).first()
        return keyword.keyword if keyword else "-"

    @admin.display(description="Extraction status")
    def extraction_status(self, obj):
        return obj.get_status_display()

    @admin.display(description="Flipkart search status")
    def flipkart_search_status(self, obj):
        return "Completed" if obj.flipkart_results.exists() else "Not searched"

    @admin.display(description="Flipkart candidates")
    def flipkart_candidates(self, obj):
        count = obj.flipkart_results.count()
        if not count:
            return "0"
        url = reverse("admin:importer_flipkartsearchresult_changelist")
        url = f"{url}?q={obj.asin}"
        return format_html('<a href="{}">{}</a>', url, count)


class ProductMatchBatchFilter(admin.SimpleListFilter):
    title = "Batch"
    parameter_name = "batch"

    def lookups(self, request, model_admin):
        return ImportBatch.objects.order_by("-created_at").values_list("pk", "keyword__keyword")

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(batches=self.value())
        return queryset


@admin.register(ProductMatch)
class ProductMatchAdmin(admin.ModelAdmin):
    form = ProductMatchAdminForm
    change_form_template = "admin/importer/productmatch/change_form.html"
    list_display = (
        "amazon_asin", "amazon_title", "flipkart_pid", "flipkart_title",
        "score", "confidence", "match_status", "publish_category_display",
        "batch_display", "published", "updated_at",
    )
    search_fields = (
        "amazon_product__asin", "amazon_product__product_title",
        "flipkart_product__pid", "flipkart_product__product_title",
        "batches__keyword__keyword",
    )
    list_filter = ("confidence", "match_status", "publish_category", PublishedProductFilter, ProductMatchBatchFilter)
    ordering = ("-score", "-updated_at")
    actions = (
        "run_product_matching",
        "assign_publish_categories",
        "approve_and_publish",
    )
    readonly_fields = (
        "amazon_asin", "amazon_title", "amazon_brand", "amazon_storage", "amazon_ram",
        "amazon_model", "amazon_color", "amazon_processor", "amazon_display", "amazon_image", "amazon_url", "amazon_price",
        "flipkart_pid", "flipkart_title", "flipkart_brand", "flipkart_storage", "flipkart_ram",
        "flipkart_model", "flipkart_color", "flipkart_processor", "flipkart_display", "flipkart_image", "flipkart_url", "flipkart_price",
        "reasons_readable", "match_status", "publish_error", "approved_by", "approved_at",
        "published_by", "published_at", "created_at", "updated_at",
        "published_product_link", "publish_category_display", "published",
    )
    fieldsets = (
        ("Products", {"fields": ("amazon_product", "flipkart_product", "published_product_link")}),
        ("Amazon product", {"fields": ("amazon_asin", "amazon_title", "amazon_brand", "amazon_model", "amazon_storage", "amazon_ram", "amazon_color", "amazon_processor", "amazon_display", "amazon_image", "amazon_url", "amazon_price")}),
        ("Flipkart product", {"fields": ("flipkart_pid", "flipkart_title", "flipkart_brand", "flipkart_model", "flipkart_storage", "flipkart_ram", "flipkart_color", "flipkart_processor", "flipkart_display", "flipkart_image", "flipkart_url", "flipkart_price")}),
        ("Match result", {"fields": ("score", "confidence", "match_status", "reasons_readable", "publish_error", "approved_by", "approved_at", "published_by", "published_at", "created_at", "updated_at")}),
        ("Publishing", {"fields": ("publish_categories", "publish_category_display", "published")}),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        match = form.instance
        categories = list(match.publish_categories.all())
        if categories and match.publish_category_id != categories[0].pk:
            match.publish_category = categories[0]
            match.save(update_fields=["publish_category", "updated_at"])

    def response_change(self, request, obj):
        if "_save_category" in request.POST:
            self.message_user(request, "Publish category saved successfully.")
        elif "_publish_product" in request.POST:
            outcome, result = self._publish_one(request, obj)
            if outcome == "published":
                self.message_user(request, f"Product #{result.product.pk} published successfully.")
            elif outcome == "already_published":
                self.message_user(request, "Product is already published; prices updated.")
            else:
                self.message_user(request, result, level=messages.ERROR)
        return super().response_change(request, obj)

    def _publish_one(self, request, product_match):
        if product_match.match_status in {"pending", "rejected"}:
            return "skipped", "This ProductMatch is not an approvable match."
        selected_categories = list(product_match.publish_categories.all())
        if not selected_categories and product_match.publish_category_id:
            selected_categories = [product_match.publish_category]
        if not selected_categories:
            return "skipped", "Please select at least one active category before publishing."
        if any(not category.is_active for category in selected_categories):
            return "skipped", "One or more selected categories are inactive. Please select active categories."
        product_match.publish_category = selected_categories[0]
        if product_match.match_status != "published":
            product_match.match_status = "approved"
            product_match.approved_by = request.user if request.user.is_authenticated else None
            product_match.approved_at = timezone.now()
            product_match.publish_error = ""
        product_match.save(update_fields=["publish_category", "match_status", "approved_by", "approved_at", "publish_error", "updated_at"])
        try:
            result = publish_product_match(product_match, user=request.user)
        except Exception as exc:
            return "failed", f"Product could not be published: {exc}"
        return ("already_published" if result.already_published else "published"), result

    @admin.action(description="Approve & Publish")
    def approve_and_publish(self, request, queryset):
        successful = failed = skipped = 0
        for product_match in queryset.select_related(
            "amazon_product", "flipkart_product", "publish_category", "published_product"
        ):
            outcome, result = self._publish_one(request, product_match)
            if outcome in {"published", "already_published"}:
                successful += 1
            elif outcome == "failed":
                failed += 1
                self.message_user(request, f"{product_match}: {result}", level=messages.ERROR)
            else:
                skipped += 1
                self.message_user(request, f"{product_match}: {result}", level=messages.WARNING)
        self.message_user(
            request,
            f"Approval and publishing completed. Successful: {successful} "
            f"Failed: {failed} Skipped: {skipped}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )

    @admin.action(description="Assign Publish Category")
    def assign_publish_categories(self, request, queryset):
        if "apply_categories" in request.POST:
            form = AssignPublishCategoriesForm(request.POST)
            if form.is_valid():
                categories = list(form.cleaned_data["categories"])
                primary = categories[0]
                for product_match in queryset:
                    product_match.publish_categories.set(categories)
                    product_match.publish_category = primary
                    product_match.save(update_fields=["publish_category", "updated_at"])
                self.message_user(
                    request,
                    f"Publish categories saved successfully for {queryset.count()} matches.",
                )
                return None
        else:
            form = AssignPublishCategoriesForm()
        context = {
            **self.admin_site.each_context(request),
            "title": "Assign publish categories",
            "form": form,
            "selected_matches": queryset,
            "action_name": "assign_publish_categories",
        }
        from django.template.response import TemplateResponse
        return TemplateResponse(
            request,
            "admin/importer/productmatch/assign_categories.html",
            context,
        )

    @admin.action(description="Run Product Matching")
    def run_product_matching(self, request, queryset):
        successful = failed = 0
        for product_match in queryset.select_related("amazon_product", "flipkart_product"):
            try:
                result = match_products(product_match.amazon_product, product_match.flipkart_product)
                ProductMatch.objects.update_or_create(
                    amazon_product=product_match.amazon_product,
                    flipkart_product=product_match.flipkart_product,
                    defaults=result,
                )
                successful += 1
            except Exception as exc:
                failed += 1
                self.message_user(request, f"{product_match}: matching failed: {exc}", level=messages.ERROR)
        self.message_user(
            request,
            f"Matching completed. Successful: {successful} Failed: {failed}.",
            level=messages.ERROR if failed else messages.SUCCESS,
        )

    def _amazon_value(self, obj, field):
        return getattr(obj.amazon_product, field, "") or "-"

    def _flipkart_value(self, obj, field):
        return getattr(obj.flipkart_product, field, "") or "-"

    @admin.display(description="Publish category")
    def publish_category_display(self, obj):
        categories = list(obj.publish_categories.all())
        if categories:
            return ", ".join(category.name for category in categories)
        return obj.publish_category.name if obj.publish_category_id else "-"

    @admin.display(description="Batch")
    def batch_display(self, obj):
        batches = list(obj.batches.select_related("keyword").all())
        if not batches:
            return "-"
        return ", ".join(batch.keyword.keyword for batch in batches)

    @admin.display(boolean=True, description="Published")
    def published(self, obj):
        return obj.match_status == "published"

    @admin.display(description="Live Product")
    def published_product_link(self, obj):
        product = getattr(obj, "published_product", None)
        if not product:
            return "-"
        url = reverse("admin:products_product_change", args=[product.pk])
        return format_html('<a href="{}">View Product</a>', url)

    @admin.display(description="Amazon ASIN")
    def amazon_asin(self, obj):
        url = reverse("admin:importer_amazonproduct_change", args=[obj.amazon_product.pk])
        return format_html('<a href="{}">{}</a>', url, obj.amazon_product.asin)
    @admin.display(description="Amazon title")
    def amazon_title(self, obj): return obj.amazon_product.product_title or "-"
    @admin.display(description="Amazon model")
    def amazon_model(self, obj): return extract_model_identity(obj.amazon_product) or "-"
    @admin.display(description="Amazon brand")
    def amazon_brand(self, obj): return self._amazon_value(obj, "brand")
    @admin.display(description="Amazon storage")
    def amazon_storage(self, obj): return self._amazon_value(obj, "storage")
    @admin.display(description="Amazon RAM")
    def amazon_ram(self, obj): return self._amazon_value(obj, "ram")
    @admin.display(description="Amazon color")
    def amazon_color(self, obj): return self._amazon_value(obj, "color")
    @admin.display(description="Amazon processor")
    def amazon_processor(self, obj): return self._amazon_value(obj, "processor")
    @admin.display(description="Amazon display")
    def amazon_display(self, obj): return self._amazon_value(obj, "display_size")
    @admin.display(description="Amazon price")
    def amazon_price(self, obj): return self._amazon_value(obj, "current_selling_price_inr")
    @admin.display(description="Amazon image")
    def amazon_image(self, obj):
        images = obj.amazon_product.images or []
        if not images:
            return "-"
        return format_html('<img src="{}" alt="Amazon product" width="96" />', images[0])
    @admin.display(description="Amazon URL")
    def amazon_url(self, obj):
        return format_html('<a href="{}" target="_blank">Open Amazon page</a>', obj.amazon_product.url)
    @admin.display(description="Flipkart PID")
    def flipkart_pid(self, obj):
        url = reverse("admin:importer_flipkartproduct_change", args=[obj.flipkart_product.pk])
        return format_html('<a href="{}">{}</a>', url, obj.flipkart_product.pid)
    @admin.display(description="Flipkart title")
    def flipkart_title(self, obj): return obj.flipkart_product.product_title or "-"
    @admin.display(description="Flipkart model")
    def flipkart_model(self, obj): return extract_model_identity(obj.flipkart_product) or "-"
    @admin.display(description="Flipkart brand")
    def flipkart_brand(self, obj): return self._flipkart_value(obj, "brand")
    @admin.display(description="Flipkart storage")
    def flipkart_storage(self, obj): return self._flipkart_value(obj, "storage")
    @admin.display(description="Flipkart RAM")
    def flipkart_ram(self, obj): return self._flipkart_value(obj, "ram")
    @admin.display(description="Flipkart color")
    def flipkart_color(self, obj): return self._flipkart_value(obj, "color")
    @admin.display(description="Flipkart processor")
    def flipkart_processor(self, obj): return self._flipkart_value(obj, "processor")
    @admin.display(description="Flipkart display")
    def flipkart_display(self, obj): return self._flipkart_value(obj, "display_size")
    @admin.display(description="Flipkart price")
    def flipkart_price(self, obj): return self._flipkart_value(obj, "current_selling_price_inr")
    @admin.display(description="Flipkart image")
    def flipkart_image(self, obj):
        images = obj.flipkart_product.images or []
        if not images:
            return "-"
        return format_html('<img src="{}" alt="Flipkart product" width="96" />', images[0])
    @admin.display(description="Flipkart URL")
    def flipkart_url(self, obj):
        return format_html('<a href="{}" target="_blank">Open Flipkart page</a>', obj.flipkart_product.url)

    @admin.display(description="Reasons")
    def reasons_readable(self, obj):
        lines = []
        for name, reason in (obj.reasons or {}).items():
            if not isinstance(reason, dict):
                lines.append(f"{name}: {reason}")
                continue
            state = "matched" if reason.get("matched") else "different"
            lines.append(
                f"{name}: {state}; score={reason.get('score', 0)}; "
                f"Amazon={reason.get('amazon') or '-'}; Flipkart={reason.get('flipkart') or '-'}"
            )
        return format_html("<pre>{}</pre>", "\n".join(lines) or "No reasons recorded.")
