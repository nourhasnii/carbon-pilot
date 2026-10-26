from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def get_company(user):
    """La company vient TOUJOURS du profil serveur, jamais d'un paramètre du navigateur."""
    from apps.carbon.services.tenancy import company_for_user
    return company_for_user(user)


class CompanyRequiredMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Login + permission Django + entreprise rattachée. Sinon 403 (jamais de redirection en boucle)."""

    raise_exception = True

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and get_company(request.user) is None:
            raise PermissionDenied("Aucune entreprise rattachée à ce compte.")
        return super().dispatch(request, *args, **kwargs)

    def handle_no_permission(self):
        # Anonyme -> page de connexion ; connecté sans droit -> 403.
        if not self.request.user.is_authenticated:
            return redirect_to_login(self.request.get_full_path(), self.get_login_url(), self.get_redirect_field_name())
        raise PermissionDenied

    @property
    def company(self):
        return get_company(self.request.user)
