from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from classifier.models import Product


class ProductUploadTests(TestCase):
    def test_xlsx_uploaded_from_dashboard_is_imported(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Product Number", "Product Name", "Product Category"])
        sheet.append(["SKU-100", "Upload-only product", "Furniture"])
        stream = BytesIO()
        workbook.save(stream)

        upload = SimpleUploadedFile(
            "products.xlsx",
            stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        with patch("classifier.views.call_command") as classify:
            response = self.client.post(reverse("upload-products"), {"file": upload})

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(Product.objects.filter(product_number="SKU-100").exists())
        classify.assert_called_once_with("classify_products")
