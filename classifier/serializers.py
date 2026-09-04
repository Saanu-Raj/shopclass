from rest_framework import serializers

from classifier.models import Product, Classification, TaxonomyCategory


class TaxonomyCategoryBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxonomyCategory
        fields = ["id", "gid", "full_name"]


class ClassificationSerializer(serializers.ModelSerializer):
    category = TaxonomyCategoryBriefSerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=TaxonomyCategory.objects.all(), write_only=True, required=False
    )

    class Meta:
        model = Classification
        fields = [
            "id", "category", "category_id", "confidence", "alternative_categories",
            "attributes", "needs_review", "review_reasons", "approval_status", "updated_at",
        ]
        read_only_fields = ["confidence", "alternative_categories", "review_reasons", "updated_at"]


class ProductSerializer(serializers.ModelSerializer):
    classification = ClassificationSerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "product_number", "model_number", "source_category", "source_sub_category",
            "brand_or_collection", "color", "name", "description", "materials", "image_urls",
            "status", "error_message", "classification",
        ]
