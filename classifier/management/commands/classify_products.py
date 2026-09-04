"""
Runs classification over all pending products in configurable batches.
Resumable by design: each product's `status` field is updated as it's
processed, so re-running this command only picks up products that are
still `pending` or stuck in `processing` (e.g. after a crash) or `failed`.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from classifier.models import Product, Classification, TaxonomyCategory
from classifier.engine import classify_product, detect_attributes


class Command(BaseCommand):
    help = "Classifies pending products in batches. Safe to re-run/resume."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--limit", type=int, default=None, help="Stop after N products total (for testing)")
        parser.add_argument("--retry-failed", action="store_true", help="Also retry previously failed products")

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        limit = options["limit"]

        statuses = [Product.STATUS_PENDING, Product.STATUS_PROCESSING]
        if options["retry_failed"]:
            statuses.append(Product.STATUS_FAILED)

        queryset = Product.objects.filter(status__in=statuses).order_by("id")
        total = queryset.count()
        self.stdout.write(f"{total} products to classify (resuming from where we left off)...")

        processed = 0
        succeeded = 0
        failed = 0

        while True:
            batch = list(queryset[:batch_size])
            if not batch:
                break
            if limit and processed >= limit:
                break

            ids = [p.id for p in batch]
            Product.objects.filter(id__in=ids).update(status=Product.STATUS_PROCESSING)

            for product in batch:
                if limit and processed >= limit:
                    break
                processed += 1
                try:
                    with transaction.atomic():
                        result = classify_product(product)
                        category = None
                        attrs = {}
                        if result.best:
                            category = TaxonomyCategory.objects.get(id=result.best.category_id)
                            attrs = detect_attributes(product, category)

                        Classification.objects.update_or_create(
                            product=product,
                            defaults=dict(
                                category=category,
                                confidence=result.best.score if result.best else 0.0,
                                alternative_categories=[
                                    {"gid": a.gid, "full_name": a.full_name, "confidence": round(a.score, 4)}
                                    for a in result.alternatives
                                ],
                                attributes=attrs,
                                needs_review=result.needs_review,
                                review_reasons=result.review_reasons,
                            ),
                        )
                        product.status = Product.STATUS_DONE
                        product.error_message = ""
                        product.save(update_fields=["status", "error_message"])
                    succeeded += 1
                except Exception as exc:
                    # Never let one bad product stop the batch.
                    failed += 1
                    product.status = Product.STATUS_FAILED
                    product.error_message = str(exc)[:2000]
                    product.save(update_fields=["status", "error_message"])
                    self.stderr.write(f"FAILED product {product.product_number}: {exc}")

            self.stdout.write(f"  ...{processed}/{total if not limit else min(limit, total)} processed")
            queryset = Product.objects.filter(status__in=statuses).order_by("id")

        self.stdout.write(self.style.SUCCESS(
            f"Done. Processed: {processed}, succeeded: {succeeded}, failed: {failed}."
        ))
