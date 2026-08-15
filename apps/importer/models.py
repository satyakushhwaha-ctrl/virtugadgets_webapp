import uuid

from django.conf import settings
from django.db import models

from apps.categories.models import Category
from apps.products.models import Product


class ImportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ApprovalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class BatchStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    AMAZON_SEARCH = "amazon_search", "Amazon Search"
    AMAZON_EXTRACTION = "amazon_extraction", "Amazon Extraction"
    FLIPKART_SEARCH = "flipkart_search", "Flipkart Search"
    FLIPKART_EXTRACTION = "flipkart_extraction", "Flipkart Extraction"
    MATCHING = "matching", "Matching"
    READY_FOR_REVIEW = "ready_for_review", "Ready for Review"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class ImporterJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    PARTIAL = "partial", "Partial"
    SKIPPED = "skipped", "Skipped"
    CANCELLED = "cancelled", "Cancelled"


class ImporterJobType(models.TextChoices):
    AMAZON_SEARCH = "amazon_search", "Amazon Search"
    AMAZON_PRODUCT_EXTRACTION = "amazon_product_extraction", "Amazon Product Extraction"
    FLIPKART_SEARCH = "flipkart_search", "Flipkart Search"
    FLIPKART_PRODUCT_EXTRACTION = "flipkart_product_extraction", "Flipkart Product Extraction"
    BEST_MATCH_FLIPKART = "best_match_flipkart", "Best-Matched Flipkart"
    PRODUCT_MATCHING = "product_matching", "Product Matching"
    PRODUCT_PUBLISHING = "product_publishing", "Product Publishing"


class ImporterJobMarketplace(models.TextChoices):
    AMAZON = "amazon", "Amazon"
    FLIPKART = "flipkart", "Flipkart"
    BOTH = "both", "Amazon + Flipkart"
    INTERNAL = "internal", "Internal"


class MatchStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    MATCHED = "matched", "Matched"
    REJECTED = "rejected", "Rejected"
    REVIEW = "review", "Review"
    APPROVED = "approved", "Approved"
    PUBLISHED = "published", "Published"
    PUBLISH_FAILED = "publish_failed", "Publish failed"


class MatchConfidence(models.TextChoices):
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


class SearchKeyword(models.Model):
    keyword = models.CharField(max_length=255, unique=True)

    status = models.CharField(
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.PENDING,
    )

    matching_status = models.CharField(
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.PENDING,
        help_text="Status of matching completed staging products for this keyword.",
    )

    total_results = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.keyword


class ImportBatch(models.Model):
    """One complete, traceable staging import job for a SearchKeyword."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyword = models.ForeignKey(
        SearchKeyword,
        on_delete=models.PROTECT,
        related_name="import_batches",
    )
    status = models.CharField(
        max_length=30,
        choices=BatchStatus.choices,
        default=BatchStatus.PENDING,
        db_index=True,
    )
    amazon_results_count = models.PositiveIntegerField(default=0)
    amazon_products_count = models.PositiveIntegerField(default=0)
    flipkart_results_count = models.PositiveIntegerField(default=0)
    flipkart_products_count = models.PositiveIntegerField(default=0)
    matches_count = models.PositiveIntegerField(default=0)
    successful_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.keyword.keyword} · {self.pk}"


class ImporterJob(models.Model):
    """Persistent lifecycle and progress record for a Celery import job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    job_type = models.CharField(max_length=40, choices=ImporterJobType.choices, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=ImporterJobStatus.choices,
        default=ImporterJobStatus.PENDING,
        db_index=True,
    )
    celery_task_id = models.CharField(max_length=255, blank=True, db_index=True)
    marketplace = models.CharField(
        max_length=20,
        choices=ImporterJobMarketplace.choices,
        blank=True,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="importer_jobs",
    )
    import_batch = models.ForeignKey(
        "ImportBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="importer_jobs",
    )
    amazon_product = models.ForeignKey(
        "AmazonProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="importer_jobs",
    )
    flipkart_product = models.ForeignKey(
        "FlipkartProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="importer_jobs",
    )
    product_match = models.ForeignKey(
        "ProductMatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="importer_jobs",
    )
    total_items = models.PositiveIntegerField(default=0)
    processed_items = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    retry_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    result_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at", "status"]),
            models.Index(fields=["job_type", "status"]),
        ]

    def __str__(self):
        return self.title

    @property
    def progress_percent(self):
        if not self.total_items:
            return 0
        return min(100, round(self.processed_items * 100 / self.total_items))

    @property
    def progress_display(self):
        return f"{self.processed_items} / {self.total_items} ({self.progress_percent}%)"


class AmazonSearchResult(models.Model):
    keyword = models.ForeignKey(
        SearchKeyword,
        on_delete=models.CASCADE,
        related_name="amazon_results",
    )

    asin = models.CharField(max_length=20, db_index=True)

    title = models.TextField()

    product_url = models.URLField(max_length=1000)

    position = models.PositiveIntegerField()

    sponsored = models.BooleanField(default=False)

    processed = models.BooleanField(default=False)
    batches = models.ManyToManyField(
        "ImportBatch",
        blank=True,
        related_name="amazon_search_results",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("keyword", "asin")
        ordering = ["position"]

    def __str__(self):
        return self.title


class AmazonProduct(models.Model):
    """Staging data extracted from an Amazon product detail page."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asin = models.CharField(max_length=20, unique=True)
    product_title = models.TextField(blank=True)
    brand = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=1000)
    availability = models.CharField(max_length=255, blank=True)
    images = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)
    highlights = models.JSONField(default=list, blank=True)
    specifications = models.JSONField(default=dict, blank=True)

    mrp_inr = models.PositiveIntegerField(null=True, blank=True)
    current_selling_price_inr = models.PositiveIntegerField(null=True, blank=True)
    selling_price_min_inr = models.PositiveIntegerField(null=True, blank=True)
    selling_price_max_inr = models.PositiveIntegerField(null=True, blank=True)
    discount_percentage = models.PositiveIntegerField(null=True, blank=True)

    primary_seller = models.CharField(max_length=255, blank=True)
    seller_rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
    )

    processor = models.CharField(max_length=255, blank=True)
    ram = models.CharField(max_length=255, blank=True)
    storage = models.CharField(max_length=255, blank=True)
    operating_system = models.CharField(max_length=255, blank=True)
    display_size = models.CharField(max_length=255, blank=True)
    resolution = models.CharField(max_length=255, blank=True)
    color = models.CharField(max_length=255, blank=True)
    weight_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )
    software = models.CharField(max_length=255, blank=True)
    warranty = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.PENDING,
        db_index=True,
    )
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    published = models.BooleanField(default=False, db_index=True)
    categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="amazon_products",
        help_text="Categories selected for the public catalog; the first is primary.",
    )
    published_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="amazon_products",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_amazon_products",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    batches = models.ManyToManyField(
        "ImportBatch",
        blank=True,
        related_name="amazon_products",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.product_title or self.asin


class FlipkartSearchResult(models.Model):
    """A Flipkart candidate found via keyword search or for an Amazon staging product."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    search_keyword = models.ForeignKey(
        SearchKeyword,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="flipkart_results",
    )
    amazon_product = models.ForeignKey(
        AmazonProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flipkart_results",
    )
    pid = models.CharField(max_length=100, db_index=True)
    title = models.TextField()
    product_url = models.URLField(max_length=1000)
    position = models.PositiveIntegerField()
    sponsored = models.BooleanField(default=False)
    processed = models.BooleanField(default=False)
    batches = models.ManyToManyField(
        "ImportBatch",
        blank=True,
        related_name="flipkart_search_results",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["search_keyword", "pid"],
                name="unique_flipkart_candidate_per_keyword",
                condition=models.Q(search_keyword__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["amazon_product", "pid"],
                name="unique_flipkart_candidate_per_amazon_product",
                condition=models.Q(amazon_product__isnull=False),
            ),
        ]
        ordering = ["position"]

    def __str__(self):
        return self.title


class FlipkartProduct(models.Model):
    """Structured Flipkart product details held in staging."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    search_result = models.OneToOneField(
        FlipkartSearchResult,
        on_delete=models.CASCADE,
        related_name="flipkart_product",
    )
    pid = models.CharField(max_length=100, unique=True)
    product_title = models.TextField(blank=True)
    brand = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=1000)
    availability = models.CharField(max_length=255, blank=True)
    images = models.JSONField(default=list, blank=True)

    mrp_inr = models.PositiveIntegerField(null=True, blank=True)
    current_selling_price_inr = models.PositiveIntegerField(null=True, blank=True)
    selling_price_min_inr = models.PositiveIntegerField(null=True, blank=True)
    selling_price_max_inr = models.PositiveIntegerField(null=True, blank=True)
    discount_percentage = models.PositiveIntegerField(null=True, blank=True)

    primary_seller = models.CharField(max_length=255, blank=True)
    seller_rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
    )

    processor = models.CharField(max_length=255, blank=True)
    ram = models.CharField(max_length=255, blank=True)
    storage = models.CharField(max_length=255, blank=True)
    operating_system = models.CharField(max_length=255, blank=True)
    display_size = models.CharField(max_length=255, blank=True)
    resolution = models.CharField(max_length=255, blank=True)
    color = models.CharField(max_length=255, blank=True)
    weight_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )
    software = models.CharField(max_length=255, blank=True)
    warranty = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.PENDING,
        db_index=True,
    )
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    published = models.BooleanField(default=False, db_index=True)
    categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="flipkart_products",
        help_text="Categories selected for the public catalog; the first is primary.",
    )
    published_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flipkart_products",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_flipkart_products",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    batches = models.ManyToManyField(
        "ImportBatch",
        blank=True,
        related_name="flipkart_products",
    )
    error_message = models.TextField(blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.product_title or self.pid


class ProductMatch(models.Model):
    """Deterministic Amazon/Flipkart match decision held in staging."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amazon_product = models.ForeignKey(
        AmazonProduct,
        on_delete=models.CASCADE,
        related_name="product_matches",
    )
    flipkart_product = models.ForeignKey(
        FlipkartProduct,
        on_delete=models.CASCADE,
        related_name="product_matches",
    )
    score = models.PositiveSmallIntegerField(default=0)
    confidence = models.CharField(
        max_length=10,
        choices=MatchConfidence.choices,
        default=MatchConfidence.LOW,
    )
    match_status = models.CharField(
        max_length=20,
        choices=MatchStatus.choices,
        default=MatchStatus.PENDING,
    )
    reasons = models.JSONField(default=dict, blank=True)
    batches = models.ManyToManyField(
        "ImportBatch",
        blank=True,
        related_name="product_matches",
    )
    publish_category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="importer_product_matches",
        help_text="Active category required before publishing to the live catalog.",
    )
    publish_categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="importer_product_match_categories",
        help_text="Active categories selected during review; the first is primary.",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_importer_product_matches",
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_importer_product_matches",
    )
    published_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="importer_product_matches",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    publish_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["amazon_product", "flipkart_product"],
                name="unique_product_match_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=0, score__lte=100),
                name="product_match_score_between_0_and_100",
            ),
        ]
        ordering = ["-score", "-updated_at"]

    def __str__(self):
        return f"{self.amazon_product.asin} ↔ {self.flipkart_product.pid}"
