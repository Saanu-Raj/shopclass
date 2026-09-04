from django.db import models


class TaxonomyAttribute(models.Model):
    """A Shopify taxonomy attribute, e.g. 'Color', 'Material'."""
    gid = models.CharField(max_length=100, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    handle = models.CharField(max_length=200)
    values = models.JSONField(default=list)  # list of allowed value strings

    def __str__(self):
        return self.name


class TaxonomyCategory(models.Model):
    """A node in the Shopify product taxonomy tree."""
    gid = models.CharField(max_length=150, unique=True, db_index=True)
    level = models.IntegerField()
    name = models.CharField(max_length=255)
    full_name = models.CharField(max_length=500, db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    attributes = models.ManyToManyField(TaxonomyAttribute, blank=True, related_name="categories")
    # Precomputed lowercase text used for matching (name + full_name).
    search_text = models.TextField(blank=True, default="")

    def __str__(self):
        return self.full_name


class Product(models.Model):
    """A row imported from the merchant's product list."""
    product_number = models.CharField(max_length=100, unique=True, db_index=True)
    model_number = models.CharField(max_length=100, blank=True, default="")
    source_category = models.CharField(max_length=255, blank=True, default="")
    source_sub_category = models.CharField(max_length=255, blank=True, default="")
    brand_or_collection = models.CharField(max_length=255, blank=True, default="")
    color = models.CharField(max_length=255, blank=True, default="")
    name = models.CharField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")
    bullets = models.TextField(blank=True, default="")
    materials = models.CharField(max_length=500, blank=True, default="")
    image_urls = models.JSONField(default=list, blank=True)
    raw_row = models.JSONField(default=dict, blank=True)  # full original row, for reference

    # Processing state, used for resumable batch runs.
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    error_message = models.TextField(blank=True, default="")

    def __str__(self):
        return self.name or self.product_number


class Classification(models.Model):
    """The classification result for one product."""
    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="classification")
    category = models.ForeignKey(
        TaxonomyCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    confidence = models.FloatField(default=0.0)  # 0.0 - 1.0
    alternative_categories = models.JSONField(default=list, blank=True)  # [{gid, full_name, confidence}]
    attributes = models.JSONField(default=dict, blank=True)  # {attribute_name: {value, confidence}}
    needs_review = models.BooleanField(default=True, db_index=True)
    review_reasons = models.JSONField(default=list, blank=True)  # e.g. ["low_confidence", "missing_description"]

    # Human review / approval workflow.
    APPROVAL_UNREVIEWED = "unreviewed"
    APPROVAL_APPROVED = "approved"
    APPROVAL_EDITED = "edited"
    APPROVAL_CHOICES = [
        (APPROVAL_UNREVIEWED, "Unreviewed"),
        (APPROVAL_APPROVED, "Approved"),
        (APPROVAL_EDITED, "Edited"),
    ]
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default=APPROVAL_UNREVIEWED)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product} -> {self.category}"
