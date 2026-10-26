from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from apps.energy.roles import (
    ENERGY_ROLE_PERMISSIONS,
    ROLE_AUDITOR,
    ROLE_DEPT_MANAGER,
    ROLE_SUSTAINABILITY_MANAGER,
)

User = get_user_model()

# Role CarbonPilot -> groupe du module Energy
ROLE_TO_GROUP = {
    User.Role.COMPANY_OWNER: ROLE_SUSTAINABILITY_MANAGER,
    User.Role.ESG_REPORTING_MANAGER: ROLE_SUSTAINABILITY_MANAGER,
    User.Role.DEPARTMENT_MANAGER: ROLE_DEPT_MANAGER,
    User.Role.AUDITOR: ROLE_AUDITOR,
}


class Command(BaseCommand):
    help = "Cree les groupes du module Energy et y rattache les utilisateurs selon leur role."

    def handle(self, *args, **options):
        for group_name, models in ENERGY_ROLE_PERMISSIONS.items():
            group, _ = Group.objects.get_or_create(name=group_name)
            perms = [
                Permission.objects.get(content_type__app_label="energy", codename=f"{action}_{model}")
                for model, actions in models.items()
                for action in actions
            ]
            group.permissions.set(perms)

        energy_groups = list(Group.objects.filter(name__in=ENERGY_ROLE_PERMISSIONS))
        count = 0
        for user in User.objects.all():
            user.groups.remove(*energy_groups)
            group_name = ROLE_TO_GROUP.get(user.role)
            if group_name:
                user.groups.add(Group.objects.get(name=group_name))
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Groupes energie synchronises : {count} utilisateur(s) rattache(s)."))