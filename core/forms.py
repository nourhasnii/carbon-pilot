from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.conf import settings


def _client_ip(request):
    return (request.META.get("REMOTE_ADDR") if request else "") or "?"


class StyledAuthenticationForm(AuthenticationForm):
    """Connexion avec classes Bootstrap, autocomplétion correcte et blocage temporaire après trop d'échecs.

    Le compteur est par (identifiant, adresse IP) : un attaquant ne peut pas verrouiller le compte
    d'un autre depuis une autre adresse, et l'utilisateur légitime n'est bloqué que 15 minutes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({
            "class": "form-control", "autocomplete": "username", "autofocus": True, "maxlength": "150",
            "autocapitalize": "none", "spellcheck": "false"})
        self.fields["password"].widget.attrs.update({"class": "form-control", "autocomplete": "current-password"})

    def _key(self, username):
        return f"login-fail:{(username or '').lower()[:150]}:{_client_ip(self.request)}"

    def clean(self):
        username = self.cleaned_data.get("username")
        key = self._key(username)
        if cache.get(key, 0) >= settings.LOGIN_MAX_ATTEMPTS:
            raise forms.ValidationError(
                "Trop de tentatives échouées. Réessayez dans quelques minutes.", code="locked")
        try:
            data = super().clean()
        except forms.ValidationError:
            try:
                cache.add(key, 0, settings.LOGIN_LOCK_SECONDS)
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, settings.LOGIN_LOCK_SECONDS)
            raise
        cache.delete(key)
        return data
