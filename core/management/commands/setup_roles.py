from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from core.roles import ENERGY_ROLE_PERMISSIONS


class Command(BaseCommand):
    help = "Crée/met à jour les groupes de rôles et leurs permissions (idempotent)."

    def handle(self, *args, **options):
        for role, models_perms in ENERGY_ROLE_PERMISSIONS.items():
            group, _ = Group.objects.get_or_create(name=role)
            perms = []
            for model, actions in models_perms.items():
                for action in actions:
                    codename = f"{action}_{model}"
                    qs = Permission.objects.filter(codename=codename, content_type__app_label="energy")
                    if qs.exists():  # les modèles forecast/scenario n'existent pas encore à ce stade
                        perms.append(qs.first())
            group.permissions.add(*perms)
            if options.get("verbosity", 1) > 0:
                self.stdout.write(self.style.SUCCESS(f"{role}: {len(perms)} permissions"))
