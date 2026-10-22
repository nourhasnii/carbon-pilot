"""Droits de l'API : mêmes groupes et permissions Django que l'interface web (une seule source de vérité)."""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from energy.mixins import get_company

METHOD_TO_ACTION = {"GET": "view", "HEAD": "view", "OPTIONS": "view", "POST": "add", "PUT": "change",
                    "PATCH": "change", "DELETE": "delete"}


class CompanyModelPermission(BasePermission):
    """Utilisateur connecté, rattaché à une entreprise, et possédant la permission Django du modèle.

    `view.model_name` = ex. « energyasset ». Une action personnalisée peut imposer sa permission via
    `view.action_permissions = {"detect": "add"}`.
    """

    message = "Vous n'avez pas le droit d'effectuer cette action."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if get_company(user) is None:
            self.message = "Aucune entreprise rattachée à ce compte."
            return False
        extra = getattr(view, "action_permissions", {})
        verb = extra.get(getattr(view, "action", None)) or METHOD_TO_ACTION.get(request.method, "change")
        return user.has_perm(f"energy.{verb}_{view.model_name}")
