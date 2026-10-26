"""Seed the shared, platform-default ESG indicator catalog.

The entries are ordinary :class:`~apps.esg.models.IndicatorCatalogEntry` rows
with ``company = None`` (shared). They are written idempotently, so re-running
the migration — or a later one that re-seeds — never duplicates a code: a
tenant can still deactivate, extend or override them afterwards.
"""

from django.db import migrations


def seed_default_catalog(apps, schema_editor):
	# Imported lazily so the migration keeps working even if the service module
	# is later re-organised; the catalog definition stays the single source.
	from apps.esg.services.catalog import default_entries

	CatalogEntry = apps.get_model("esg", "IndicatorCatalogEntry")
	for entry in default_entries():
		CatalogEntry.objects.update_or_create(
			company=None,
			code=entry["code"],
			defaults={
				"name": entry["name"],
				"pillar": entry["pillar"],
				"unit": entry["unit"],
				"description": entry["description"],
				"is_required": entry["is_required"],
				"is_active": True,
				"sort_order": entry["sort_order"],
			},
		)


def remove_default_catalog(apps, schema_editor):
	from apps.esg.services.catalog import default_entries

	CatalogEntry = apps.get_model("esg", "IndicatorCatalogEntry")
	codes = [entry["code"] for entry in default_entries()]
	CatalogEntry.objects.filter(company__isnull=True, code__in=codes).delete()


class Migration(migrations.Migration):

	dependencies = [
		("esg", "0001_initial"),
	]

	operations = [
		migrations.RunPython(seed_default_catalog, remove_default_catalog),
	]
