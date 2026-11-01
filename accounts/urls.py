from django.urls import path

from . import views

urlpatterns = [
    path("csrf/", views.csrf),
    path("me/", views.me),
    path("signup/", views.signup),
    path("login/", views.login_view),
    path("verify/", views.verify),
    path("resend/", views.resend),
    path("google/", views.google),
    path("logout/", views.logout_view),
    path("password/forgot/", views.password_forgot),
    path("password/reset/", views.password_reset),
    path("subscription/", views.subscription),
    path("payment-methods/", views.payment_methods),
    path("payments/", views.submit_payment),
    path("payments/<int:payment_id>/proof/<int:index>/", views.payment_proof),
]
