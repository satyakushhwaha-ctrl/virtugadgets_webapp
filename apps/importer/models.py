from django.db import models


class ImportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class SearchKeyword(models.Model):
    keyword = models.CharField(max_length=255, unique=True)

    status = models.CharField(
        max_length=20,
        choices=ImportStatus.choices,
        default=ImportStatus.PENDING,
    )

    total_results = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.keyword


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

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("keyword", "asin")
        ordering = ["position"]

    def __str__(self):
        return self.title