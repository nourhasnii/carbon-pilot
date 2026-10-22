from datetime import datetime, timezone
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Company, Department
from energy.models import EnergyAsset, EnergyReading


def ts(day, hour=0):
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


class EnergyModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_a = Company.objects.create(nom="Alpha")
        cls.company_b = Company.objects.create(nom="Beta")
        cls.asset_a = EnergyAsset.objects.create(company=cls.company_a, name="HVAC A", code="HV1")
        cls.asset_b = EnergyAsset.objects.create(company=cls.company_b, name="HVAC B", code="HV1")

    def test_asset_code_unique_per_company_only(self):
        # Même code dans deux entreprises : autorisé (setUpTestData). Doublon dans la même : refusé.
        with self.assertRaises(IntegrityError), transaction.atomic():
            EnergyAsset.objects.create(company=self.company_a, name="Autre", code="HV1")

    def test_department_must_belong_to_same_company(self):
        dept_b = Department.objects.create(company=self.company_b, nom="Ops", code="OPS")
        asset = EnergyAsset(company=self.company_a, name="X", code="X1", department=dept_b)
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_duplicate_reading_rejected(self):
        EnergyReading.objects.create(asset=self.asset_a, timestamp=ts(1), value=Decimal("120"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            EnergyReading.objects.create(asset=self.asset_a, timestamp=ts(1), value=Decimal("130"))

    def test_negative_value_rejected_by_validation_and_database(self):
        r = EnergyReading(asset=self.asset_a, timestamp=ts(2), value=Decimal("-1"))
        with self.assertRaises(ValidationError):
            r.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            EnergyReading.objects.create(asset=self.asset_a, timestamp=ts(3), value=Decimal("-1"))

    def test_demo_flag_defaults_to_false(self):
        r = EnergyReading.objects.create(asset=self.asset_a, timestamp=ts(4), value=Decimal("10"))
        self.assertFalse(r.is_demo)


class TenantIsolationTests(TestCase):
    def test_for_company_returns_only_own_readings(self):
        a, b = Company.objects.create(nom="A"), Company.objects.create(nom="B")
        asset_a = EnergyAsset.objects.create(company=a, name="m1", code="M1")
        asset_b = EnergyAsset.objects.create(company=b, name="m2", code="M2")
        ra = EnergyReading.objects.create(asset=asset_a, timestamp=ts(1), value=Decimal("5"))
        EnergyReading.objects.create(asset=asset_b, timestamp=ts(1), value=Decimal("9"))
        self.assertEqual(list(EnergyReading.objects.for_company(a)), [ra])
