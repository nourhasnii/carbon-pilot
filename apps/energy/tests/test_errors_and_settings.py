import os
import subprocess
import sys
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import path, reverse

from core.models import Company, UserProfile
from core.roles import ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER


class ErrorPagesTests(TestCase):
    """Pages d'erreur personnalisées : jamais de trace technique, navigation de retour proposée."""

    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.company = Company.objects.create(nom="A")
        cls.admin = User.objects.create_user("adm", password="pw-test-123")
        cls.admin.groups.add(Group.objects.get(name=ROLE_SUSTAINABILITY_MANAGER))
        UserProfile.objects.create(user=cls.admin, company=cls.company)
        cls.emp = User.objects.create_user("emp", password="pw-test-123")
        cls.emp.groups.add(Group.objects.get(name=ROLE_EMPLOYEE))
        UserProfile.objects.create(user=cls.emp, company=cls.company)

    def test_404_page(self):
        self.client.force_login(self.admin)
        r = self.client.get("/energy/n-existe-pas/")
        self.assertContains(r, "Page introuvable", status_code=404)
        self.assertNotContains(r, "Traceback", status_code=404)

    def test_403_page(self):
        self.client.force_login(self.emp)
        r = self.client.get(reverse("energy:anomaly_list"))
        self.assertEqual(r.status_code, 403)
        self.assertNotContains(r, "Traceback", status_code=403)

    def test_500_page_hides_technical_details(self):
        client = Client(raise_request_exception=False)
        client.force_login(self.admin)
        with mock.patch("energy.views.DashboardView.get_context_data", side_effect=RuntimeError("secret-interne-xyz")), \
                self.assertLogs("django.request", level="ERROR"):  # la panne est voulue : on capture son journal
            r = client.get(reverse("energy:dashboard"))
        self.assertEqual(r.status_code, 500)
        self.assertNotContains(r, "secret-interne-xyz", status_code=500)
        self.assertNotContains(r, "Traceback", status_code=500)

    def test_api_errors_are_json_without_trace(self):
        client = Client(raise_request_exception=False)
        from rest_framework.authtoken.models import Token
        tok = Token.objects.create(user=self.admin).key
        r = client.get("/api/assets/999999/", HTTP_AUTHORIZATION=f"Token {tok}")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"detail": "Ressource introuvable."})
        with mock.patch("energy.api.views.AssetViewSet.list", side_effect=RuntimeError("secret-interne-xyz")), \
                self.assertLogs("energy.api.exceptions", level="ERROR"):
            r = client.get("/api/assets/", HTTP_AUTHORIZATION=f"Token {tok}")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json(), {"detail": "Erreur interne du serveur."})


class ProductionSettingsTests(TestCase):
    """En production (DEBUG=0), l'application refuse de démarrer sans clé secrète sérieuse."""

    def _run(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
        code = "import django, os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; from django.conf import settings; settings.SECRET_KEY; print('OK')"
        return subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True, text=True,
                              cwd=str(__import__("django.conf").conf.settings.BASE_DIR))

    def test_refuses_default_key_in_production(self):
        r = self._run(DJANGO_DEBUG="0")
        self.assertNotIn("OK", r.stdout)
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)

    def test_refuses_short_key_in_production(self):
        self.assertNotIn("OK", self._run(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="court").stdout)

    def test_accepts_strong_key_in_production(self):
        r = self._run(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="x" * 50)
        self.assertIn("OK", r.stdout, r.stderr)

    def test_dev_mode_needs_nothing(self):
        self.assertIn("OK", self._run().stdout)
