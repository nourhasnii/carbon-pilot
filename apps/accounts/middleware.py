from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

from .permissions import has_active_company_access


class PasswordChangeRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not has_active_company_access(user):
            logout(request)
            return redirect("accounts:login")
        if user.is_authenticated and getattr(user, "must_change_password", False):
            allowed_paths = {
                reverse("accounts:force_password_change"),
                reverse("accounts:logout"),
            }
            static_url = "/" + settings.STATIC_URL.lstrip("/")
            media_url = "/" + settings.MEDIA_URL.lstrip("/")
            if (
                request.path_info not in allowed_paths
                and not request.path.startswith((static_url, media_url))
            ):
                return redirect("accounts:force_password_change")
        return self.get_response(request)