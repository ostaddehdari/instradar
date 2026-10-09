from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("instradar/admin/", admin.site.urls),
    path("instradar/login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("instradar/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("instradar/", include("radar.urls")),
]
