from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .models import User
from apps.enterprise.models import Company


def has_active_company_access(user):
    if user.role == User.Role.SUPER_ADMIN:
        return True

    if user.role == User.Role.COMPANY_OWNER:
        company = Company.objects.filter(owner_id=user.pk).only("is_active").first()
    elif user.company_membership_id:
        company = Company.objects.filter(pk=user.company_membership_id).only("is_active").first()
    elif user.company:
        return not Company.objects.filter(name__iexact=user.company, is_active=False).exists()
    else:
        company = None

    return company is None or company.is_active


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if user.status != User.Status.ACTIVE:
                raise PermissionDenied
            if not has_active_company_access(user):
                raise PermissionDenied
            if user.role != User.Role.SUPER_ADMIN and user.role not in roles:
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator