from django.db.models import Count, Avg, Q
from django.core.paginator import Paginator
from django.shortcuts import render, redirect
from django.core.management import call_command
from django.contrib import messages
from rest_framework import viewsets, filters
from rest_framework.decorators import action
from rest_framework.response import Response

from classifier.models import Product, Classification, TaxonomyCategory
from classifier.serializers import ProductSerializer, ClassificationSerializer
from classifier.importers import import_products_from_workbook, ImportError_


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
    Accepts an uploaded Excel product list, imports it, then classifies
    just the newly-added (pending) products -- so uploading a small batch
    on top of an already-classified catalogue doesn't re-run everything.
    """
    if request.method != "POST" or not request.FILES.get("file"):
        messages.error(request, "Please choose an .xlsx file to upload.")
        return redirect("dashboard")

    upload = request.FILES["file"]
    if not upload.name.lower().endswith((".xlsx", ".xlsm")):
        messages.error(request, f"'{upload.name}' doesn't look like an Excel (.xlsx) file.")
        return redirect("dashboard")

    try:
        created, skipped, warnings = import_products_from_workbook(upload)
    except ImportError_ as exc:
        messages.error(request, f"Import failed: {exc}")
        return redirect("dashboard")
    except Exception as exc:
        messages.error(request, f"Unexpected error while reading the file: {exc}")
        return redirect("dashboard")

    for w in warnings:
        messages.warning(request, w)

    if created == 0:
        messages.warning(request, f"No new products were imported (skipped {skipped} row(s) with no Product Number).")
        return redirect("dashboard")

    # Classify only what's pending -- reuses the same tested, resumable command.
    call_command("classify_products")

    msg = f"Imported {created} product(s)"
    if skipped:
        msg += f" (skipped {skipped} row(s) with no Product Number)"
    msg += " and ran classification on them."
    messages.success(request, msg)
    return redirect("dashboard")


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

    return render(request, "classifier/dashboard.html", {
        "stats": stats,
        "products": page_obj,
        "page_obj": page_obj,
        "needs_review": needs_review,
        "search": search,
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
