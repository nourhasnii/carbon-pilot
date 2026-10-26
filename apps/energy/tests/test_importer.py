import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.enterprise.models import Company
from apps.energy.models import EnergyAsset, EnergyReading
from apps.energy.services.importer import ImportFileError, import_readings


def csv_file(text, name="data.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


HEADER = "asset_code,timestamp,value,unit\n"


class ImporterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Company.objects.create(nom="A")
        cls.b = Company.objects.create(nom="B")
        cls.asset = EnergyAsset.objects.create(company=cls.a, name="HVAC", code="HV1")
        cls.other = EnergyAsset.objects.create(company=cls.b, name="Autre", code="OT1")

    def messages(self, res):
        return {e.line: e.message for e in res.errors}

    def test_valid_rows_are_imported_and_batch_recorded(self):
        res = import_readings(csv_file(HEADER + "HV1,2026-01-01 10:00,120,kWh\nHV1,2026-01-01 11:00,\"130,5\",kWh\n"), self.a)
        self.assertEqual((res.total_rows, res.imported_rows, res.rejected_rows), (2, 2, 0))
        self.assertEqual(EnergyReading.objects.for_company(self.a).count(), 2)
        self.assertEqual(res.batch.imported_rows, 2)

    def test_unit_defaults_to_kwh_when_column_absent(self):
        res = import_readings(csv_file("asset_code,timestamp,value\nHV1,2026-01-01 10:00,5\n"), self.a)
        self.assertEqual(res.imported_rows, 1)
        self.assertEqual(EnergyReading.objects.get().unit, "kWh")

    def test_each_invalid_rule_reports_its_line(self):
        text = HEADER + "\n".join([
            "HV1,2026-01-01 10:00,10,kWh",   # ligne 2 : OK
            "NOPE,2026-01-01 11:00,10,kWh",  # 3 asset inconnu
            "HV1,pas-une-date,10,kWh",       # 4 date invalide
            "HV1,2026-01-01 12:00,abc,kWh",  # 5 non numérique
            "HV1,2026-01-01 13:00,-4,kWh",   # 6 négative
            "HV1,2026-01-01 14:00,10,parsec",  # 7 unité inconnue
            "HV1,2026-01-01 10:00,11,kWh",   # 8 doublon fichier
            "HV1,2099-01-01 10:00,10,kWh",   # 9 futur
            "HV1,2026-01-01 09:00,10,kWh",   # 10 ordre temporel
            "HV1,2026-01-01 15:00,1e12,kWh",  # 11 valeur impossible
        ]) + "\n"
        res = import_readings(csv_file(text), self.a)
        self.assertEqual(res.imported_rows, 1)
        self.assertEqual(sorted(self.messages(res)), [3, 4, 5, 6, 7, 8, 9, 10, 11])
        self.assertIn("inconnu", self.messages(res)[3])
        self.assertIn("négative", self.messages(res)[6])
        self.assertIn("Doublon", self.messages(res)[8])

    def test_duplicate_with_database_is_rejected(self):
        import_readings(csv_file(HEADER + "HV1,2026-01-01 10:00,10,kWh\n"), self.a)
        res = import_readings(csv_file(HEADER + "HV1,2026-01-01 10:00,10,kWh\n"), self.a)
        self.assertEqual(res.imported_rows, 0)
        self.assertIn("déjà existante", res.errors[0].message)
        self.assertEqual(EnergyReading.objects.count(), 1)

    def test_cannot_import_into_another_companys_asset(self):
        res = import_readings(csv_file(HEADER + "OT1,2026-01-01 10:00,10,kWh\n"), self.a)
        self.assertEqual(res.imported_rows, 0)
        self.assertEqual(EnergyReading.objects.count(), 0)

    def test_missing_columns_raise(self):
        with self.assertRaises(ImportFileError):
            import_readings(csv_file("asset_code,value\nHV1,1\n"), self.a)

    def test_unsupported_extension_raises(self):
        with self.assertRaises(ImportFileError):
            import_readings(csv_file("x", name="data.txt"), self.a)

    def test_excel_import(self):
        import pandas as pd
        buf = io.BytesIO()
        pd.DataFrame({"asset_code": ["HV1"], "timestamp": ["2026-01-02 10:00"], "value": ["42"], "unit": ["kWh"]}).to_excel(buf, index=False)
        f = SimpleUploadedFile("data.xlsx", buf.getvalue())
        res = import_readings(f, self.a)
        self.assertEqual(res.imported_rows, 1)
