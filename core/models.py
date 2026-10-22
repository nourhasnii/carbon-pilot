"""Modèles minimaux du socle multi-tenant.

NOTE D'INTÉGRATION : ces modèles sont un squelette temporaire pour que le module
Energy fonctionne seul. Lors de la fusion avec le travail des camarades (pilier
Enterprise Management), remplacer par leurs modèles Company / Department /
UserProfile en conservant les mêmes noms de champs utilisés par `energy`
(`user.profile.company`).
"""
from django.conf import settings
from django.db import models


class Company(models.Model):
    nom = models.CharField(max_length=150)
    siret = models.CharField(max_length=20, blank=True)
    secteur = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name_plural = "companies"

    def __str__(self):
        return self.nom


class Department(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="departments")
    nom = models.CharField(max_length=100)
    code = models.CharField(max_length=20)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_dept_code_per_company")]

    def __str__(self):
        return f"{self.nom} ({self.company})"


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="members")
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"{self.user} @ {self.company}"
