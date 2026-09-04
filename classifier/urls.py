from django.urls import path, include
from rest_framework.routers import DefaultRouter

from classifier import views

router = DefaultRouter()
router.register("products", views.ProductViewSet, basename="product")

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload_products, name="upload-products"),
    path("upload/jobs/<int:job_id>/progress/", views.classification_job_progress, name="classification-job-progress"),
    path("api/", include(router.urls)),
    path("api/categories/search/", views.category_search_api, name="category-search"),
]
