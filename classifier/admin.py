from django.contrib import admin
from classifier.models import Product, Classification, TaxonomyCategory, TaxonomyAttribute


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("product_number", "name", "source_category", "status")
    search_fields = ("product_number", "name")
    list_filter = ("status",)


@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ("product", "category", "confidence", "needs_review", "approval_status")
    list_filter = ("needs_review", "approval_status")


admin.site.register(TaxonomyCategory)
admin.site.register(TaxonomyAttribute)
