from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

from core.forms import StyledAuthenticationForm

handler403 = "core.views.error_403"
handler404 = "core.views.error_404"
handler500 = "core.views.error_500"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(authentication_form=StyledAuthenticationForm), name="login"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("energy/", include("energy.urls")),
    path("api/", include("energy.api.urls")),
    path("", RedirectView.as_view(pattern_name="energy:dashboard", permanent=False)),
]
