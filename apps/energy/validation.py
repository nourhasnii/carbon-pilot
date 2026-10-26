"""Contrôle de saisie centralisé : mêmes règles pour les formulaires, l'import, l'admin et les filtres d'URL.

Principe : on ne fait jamais confiance au navigateur. Les attributs HTML (min, max, pattern...) aident
l'utilisateur, mais ce sont ces règles, côté serveur, qui protègent les données.
"""
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

# Caractères de contrôle, espaces de largeur nulle et marques de direction (usurpation d'affichage « bidi »)
_SINGLE_LINE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁠-⁩﻿]")
_MULTILINE_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁩﻿]")
CODE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._\-]{0,39}"
_CODE_RE = re.compile(rf"^{CODE_PATTERN}$")
_ASCII_INT = re.compile(r"^[0-9]{1,9}$", re.ASCII)

MAX_READING_VALUE = Decimal("1000000000")
MIN_TIMESTAMP = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)
FUTURE_TOLERANCE = timedelta(minutes=5)  # horloges légèrement décalées

# Unités plausibles selon l'énergie de l'asset (évite « électricité en litres »)
UNITS_BY_ENERGY = {
    "electricity": {"kWh", "MWh"},
    "gas": {"kWh", "MWh", "m3"},
    "fuel": {"L", "kWh", "MWh"},
    "other": {"kWh", "MWh", "m3", "L"},
}


def validate_single_line(value):
    if value and _SINGLE_LINE_FORBIDDEN.search(value):
        raise ValidationError("Caractères de contrôle ou invisibles non autorisés.", code="control_chars")


def validate_multiline(value):
    if value and _MULTILINE_FORBIDDEN.search(value):
        raise ValidationError("Caractères de contrôle ou invisibles non autorisés.", code="control_chars")


def validate_has_alnum(value):
    if value and not any(c.isalnum() for c in value):
        raise ValidationError("Doit contenir au moins une lettre ou un chiffre.", code="no_alnum")


def validate_code(value):
    if not _CODE_RE.match(value or ""):
        raise ValidationError(
            "Code invalide : lettres, chiffres, point, tiret ou tiret bas (40 caractères maximum), "
            "sans espace, et il doit commencer par une lettre ou un chiffre.", code="bad_code")


def validate_not_future(ts):
    if ts and ts > timezone.now() + FUTURE_TOLERANCE:
        raise ValidationError("La date ne peut pas être dans le futur.", code="future")


def validate_not_too_old(ts):
    if ts and ts < MIN_TIMESTAMP:
        raise ValidationError("La date est antérieure à 2000 : vérifiez la saisie.", code="too_old")


def unit_error(energy_type, unit):
    """Message d'erreur si l'unité n'a pas de sens pour ce type d'énergie, sinon None."""
    allowed = UNITS_BY_ENERGY.get(energy_type, UNITS_BY_ENERGY["other"])
    if unit not in allowed:
        return f"L'unité « {unit} » n'est pas valable pour ce type d'énergie (autorisées : {', '.join(sorted(allowed))})."
    return None


def parse_int_param(raw, default=None, lo=1, hi=999_999_999):
    """Entier venant de l'URL, ou `default`. Rejette les chiffres non ASCII (« ² »), les signes, les valeurs géantes."""
    if raw is not None and _ASCII_INT.match(raw):
        n = int(raw)
        if lo <= n <= hi:
            return n
    return default


def clean_search(raw, max_len=100):
    """Terme de recherche : sans caractères de contrôle, tronqué."""
    raw = _SINGLE_LINE_FORBIDDEN.sub("", raw or "").strip()
    return raw[:max_len]
