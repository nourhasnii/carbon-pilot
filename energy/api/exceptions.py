"""Erreurs d'API : jamais de trace technique, messages en français, format JSON constant."""
import logging

from django.core.exceptions import PermissionDenied
from django.http import Http404
from rest_framework.response import Response
from rest_framework.views import exception_handler

log = logging.getLogger(__name__)


def handler(exc, context):
    if isinstance(exc, Http404):
        return Response({"detail": "Ressource introuvable."}, status=404)
    if isinstance(exc, PermissionDenied):
        return Response({"detail": "Accès refusé."}, status=403)
    response = exception_handler(exc, context)
    if response is None:  # erreur inattendue : on journalise, on ne divulgue rien
        log.exception("Erreur API non gérée", exc_info=exc)
        return Response({"detail": "Erreur interne du serveur."}, status=500)
    return response
