from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from classifier.jobs import process_job
from classifier.models import ClassificationJob, Product, TaxonomyCategory


class ProductUploadTests(TestCase):
    def make_upload(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Product Number", "Product Name", "Product Category"])
        sheet.append(["SKU-100", "Upload-only product", "Furniture"])
        stream = BytesIO()
        workbook.save(stream)
        return SimpleUploadedFile(
            "products.xlsx",
            stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_xlsx_upload_creates_a_background_job(self):
        with patch("classifier.views.start_job") as start_job:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("upload-products"),
                    {"file": self.make_upload()},
                    HTTP_ACCEPT="application/json",
                )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(Product.objects.filter(product_number="SKU-100").exists())
        job = ClassificationJob.objects.get()
        self.assertEqual(job.total_products, 1)
        self.assertEqual(job.products.count(), 1)
        self.assertEqual(response.json()["job_id"], job.id)
        start_job.assert_called_once_with(job.id)

    def test_progress_endpoint_returns_persisted_counts(self):
        job = ClassificationJob.objects.create(
            status=ClassificationJob.STATUS_PROCESSING,
            total_products=4,
            processed_products=1,
            classified_products=1,
            needs_review_products=1,
        )
        with patch("classifier.views.start_job"):
            response = self.client.get(reverse("classification-job-progress", args=[job.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["percent"], 25.0)
        self.assertEqual(response.json()["needs_review_products"], 1)

    def test_background_job_tracks_successes_and_failures(self):
        job = ClassificationJob.objects.create(total_products=2)
        successful = Product.objects.create(product_number="SKU-SUCCESS", import_job=job)
        failed = Product.objects.create(product_number="SKU-FAILED", import_job=job)

        def classify(product):
            if product.pk == failed.pk:
                raise RuntimeError("test classification failure")
            return True

        with patch("classifier.jobs._classify_product", side_effect=classify):
            process_job(job.id)

        job.refresh_from_db()
        successful.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual(job.status, ClassificationJob.STATUS_COMPLETED)
        self.assertEqual(job.processed_products, 2)
        self.assertEqual(job.classified_products, 1)
        self.assertEqual(job.failed_products, 1)
        self.assertEqual(job.needs_review_products, 1)
        self.assertEqual(successful.status, Product.STATUS_DONE)
        self.assertEqual(failed.status, Product.STATUS_FAILED)

    def test_background_job_uses_the_existing_classifier_result(self):
        category = TaxonomyCategory.objects.create(
            gid="test-chair-category",
            level=3,
            name="Chairs",
            full_name="Home & Garden > Furniture > Chairs",
            search_text="chairs furniture seating",
        )
        job = ClassificationJob.objects.create(total_products=1)
        product = Product.objects.create(
            product_number="SKU-CHAIR",
            name="Wooden chair",
            source_category="Furniture",
            import_job=job,
        )

        from classifier.engine import classify_product
        expected = classify_product(product)
        process_job(job.id)

        product.refresh_from_db()
        self.assertEqual(product.status, Product.STATUS_DONE)
        self.assertEqual(product.classification.category_id, expected.best.category_id)
        self.assertEqual(product.classification.confidence, expected.best.score)
