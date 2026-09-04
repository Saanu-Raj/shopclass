from django.db.models import Count, Avg, Q
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse
from rest_framework import viewsets, filters
from rest_framework.decorators import action
from rest_framework.response import Response

from classifier.models import Product, Classification, ClassificationJob, TaxonomyCategory
from classifier.serializers import ProductSerializer, ClassificationSerializer
from classifier.importers import import_products_from_workbook, ImportError_
from classifier.jobs import start_job


class ProductViewSet(viewsets.ModelViewSet):
    """
    List/search/filter products with their classification results, and
    approve or correct a classification.

    Query params on the list endpoint:
      ?needs_review=true
      ?approval_status=unreviewed|approved|edited
      ?search=sofa   (matches product name/description)
      ?min_confidence=0.5&max_confidence=0.8
    """
    queryset = Product.objects.select_related("classification", "classification__category").order_by("id")
    serializer_class = ProductSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "description", "product_number", "brand_or_collection"]
    ordering_fields = ["id", "classification__confidence"]

    def get_queryset(self):
        qs = super().get_queryset()
        needs_review = self.request.query_params.get("needs_review")
        if needs_review is not None:
            qs = qs.filter(classification__needs_review=(needs_review.lower() == "true"))
        approval_status = self.request.query_params.get("approval_status")
        if approval_status:
            qs = qs.filter(classification__approval_status=approval_status)
        min_conf = self.request.query_params.get("min_confidence")
        if min_conf:
            qs = qs.filter(classification__confidence__gte=float(min_conf))
        max_conf = self.request.query_params.get("max_confidence")
        if max_conf:
            qs = qs.filter(classification__confidence__lte=float(max_conf))
        return qs

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Mark the current (top) classification as correct."""
        product = self.get_object()
        c = product.classification
        c.approval_status = Classification.APPROVAL_APPROVED
        c.needs_review = False
        c.save(update_fields=["approval_status", "needs_review", "updated_at"])
        return Response(ClassificationSerializer(c).data)

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """Human overrides the category (e.g. picks one of the alternatives, or a new one)."""
        product = self.get_object()
        c = product.classification
        category_id = request.data.get("category_id")
        category = TaxonomyCategory.objects.get(id=category_id)
        c.category = category
        c.approval_status = Classification.APPROVAL_EDITED
        c.needs_review = False
        c.confidence = 1.0  # human-confirmed
        c.save(update_fields=["category", "approval_status", "needs_review", "confidence", "updated_at"])
        return Response(ClassificationSerializer(c).data)


def upload_products(request):
    """
    Accepts an uploaded Excel product list and queues only its newly-created
    products for background classification.
    """
    if request.method != "POST" or not request.FILES.get("file"):
        messages.error(request, "Please choose an .xlsx file to upload.")
        return redirect("dashboard")

    upload = request.FILES["file"]
    if not upload.name.lower().endswith((".xlsx", ".xlsm")):
        messages.error(request, f"'{upload.name}' doesn't look like an Excel (.xlsx) file.")
        return redirect("dashboard")

    job = ClassificationJob.objects.create()
    try:
        created, skipped, warnings = import_products_from_workbook(upload, import_job=job)
    except ImportError_ as exc:
        job.delete()
        messages.error(request, f"Import failed: {exc}")
        return redirect("dashboard")
    except Exception as exc:
        job.delete()
        messages.error(request, f"Unexpected error while reading the file: {exc}")
        return redirect("dashboard")

    for w in warnings:
        messages.warning(request, w)

    job.total_products = job.products.count()
    job.save(update_fields=["total_products"])
    if job.total_products == 0:
        job.delete()
        messages.warning(request, f"No new products were imported (skipped {skipped} row(s) with no Product Number).")
        return redirect("dashboard")

    transaction.on_commit(lambda: start_job(job.id))

    if request.headers.get("Accept") == "application/json":
        return JsonResponse({
            "job_id": job.id,
            "progress_url": reverse("classification-job-progress", args=[job.id]),
            "total_products": job.total_products,
        }, status=202)

    msg = f"Imported {job.total_products} product(s) and started classification in the background"
    if skipped:
        msg += f" (skipped {skipped} row(s) with no Product Number)"
    messages.success(request, msg)
    return redirect("dashboard")


def classification_job_progress(request, job_id):
    """Return persisted job progress and resume work after a process restart."""
    try:
        job = ClassificationJob.objects.get(pk=job_id)
    except ClassificationJob.DoesNotExist:
        return JsonResponse({"detail": "Classification job not found."}, status=404)

    if job.status in {ClassificationJob.STATUS_PENDING, ClassificationJob.STATUS_PROCESSING}:
        start_job(job.id)

    total = job.total_products
    percent = round((job.processed_products / total) * 100, 1) if total else 0
    return JsonResponse({
        "id": job.id,
        "status": job.status,
        "total_products": total,
        "processed_products": job.processed_products,
        "classified_products": job.classified_products,
        "failed_products": job.failed_products,
        "needs_review_products": job.needs_review_products,
        "percent": percent,
        "error_message": job.error_message,
    })


def dashboard(request):
    """Simple server-rendered review UI (no separate frontend build needed)."""
    stats = {
        "total": Product.objects.count(),
        "classified": Classification.objects.count(),
        "needs_review": Classification.objects.filter(needs_review=True).count(),
        "approved": Classification.objects.filter(approval_status=Classification.APPROVAL_APPROVED).count(),
        "avg_confidence": Classification.objects.aggregate(a=Avg("confidence"))["a"] or 0,
    }

    needs_review = request.GET.get("needs_review", "true") == "true"
    qs = Product.objects.select_related("classification", "classification__category").filter(
        classification__isnull=False
    )
    if needs_review:
        qs = qs.filter(classification__needs_review=True).order_by("classification__confidence")
    else:
        # Keep every classified product in this view, but place products that
        # do not need review first. Otherwise its first page is usually the
        # same low-confidence products shown in the review queue.
        qs = qs.order_by("classification__needs_review", "classification__confidence")
    search = request.GET.get("q", "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(product_number__icontains=search))
    page_obj = Paginator(qs, 100).get_page(request.GET.get("page"))
    active_job = ClassificationJob.objects.filter(
        status__in=[ClassificationJob.STATUS_PENDING, ClassificationJob.STATUS_PROCESSING]
    ).order_by("-created_at").first()

    return render(request, "classifier/dashboard.html", {
        "stats": stats,
        "products": page_obj,
        "page_obj": page_obj,
        "needs_review": needs_review,
        "search": search,
        "active_job": active_job,
    })


def category_search_api(request):
    """Lightweight endpoint the correction UI uses to search categories by name."""
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return Response_json([])
    results = list(
        TaxonomyCategory.objects.filter(full_name__icontains=q, children__isnull=True)
        .values("id", "full_name")[:20]
    )
    return Response_json(results)


def Response_json(data):
    from django.http import JsonResponse
    return JsonResponse(data, safe=False)
