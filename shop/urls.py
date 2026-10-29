from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("products", views.ProductViewSet)
router.register("variants", views.VariantViewSet)
router.register("customers", views.CustomerViewSet)
router.register("bills", views.BillViewSet)
router.register("templates", views.BillTemplateViewSet)
router.register("barcodes", views.BarcodeViewSet)
router.register("notifications", views.NotificationViewSet)

urlpatterns = [
    path("health/", views.health),
    path("settings/", views.shop_settings),
    path("voice/transcribe/", views.voice_transcribe),
    path("voice/interpret/", views.voice_interpret),
    path("", include(router.urls)),
]
