from django.urls import path
from django.contrib.auth import views as auth_views

from . import views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("waiting/", views.waiting_page, name="waiting"),
    path("approvals/", views.approval_queue, name="approval_queue"),
    path("approvals/submit/", views.approve_user, name="approve_user"),
]
