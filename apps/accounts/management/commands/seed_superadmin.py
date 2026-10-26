from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or ensure the initial CarbonPilot Super Admin account."

    def handle(self, *args, **options):
        email = settings.SUPER_ADMIN_EMAIL.strip().lower()
        password = settings.SUPER_ADMIN_PASSWORD
        if not email or not password:
            raise CommandError("Set SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD first.")

        user_model = get_user_model()
        user = user_model.objects.filter(email__iexact=email).first()
        if user is None:
            user = user_model.objects.create_superuser(
                email=email,
                password=password,
                first_name="Super",
                last_name="Admin",
            )
            self.stdout.write(self.style.SUCCESS(f"Created Super Admin: {email}"))
            return

        user.role = user_model.Role.SUPER_ADMIN
        user.status = user_model.Status.ACTIVE
        user.is_staff = True
        user.is_superuser = True
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Super Admin is ready: {email}"))