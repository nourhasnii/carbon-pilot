"""Seed a clearly identified DEMO emission-factor library.

These values are illustrative placeholders used to demonstrate the platform.
They are explicitly flagged as ``DEMO`` and their source/methodology text states
that they are not official emission factors. Replace them with verified factors
from an official source before producing real reports.

The command is idempotent: re-running it updates the demo rows in place.
"""

from django.core.management.base import BaseCommand

from apps.carbon.models import ActivityCategory, EmissionFactor, Scope, VerificationStatus

DEMO_SOURCE = "CarbonPilot demo dataset — illustrative example, NOT an official emission factor."
DEMO_METHODOLOGY = (
	"Demo value included only to demonstrate the platform. Replace it with a "
	"verified factor from an official source (e.g. national inventory, IEA, DEFRA, "
	"ADEME) before any real reporting."
)

DEMO_FACTORS = [
	{
		"name": "Electricity — Tunisia",
		"category": ActivityCategory.ELECTRICITY,
		"scope": Scope.SCOPE_2,
		"value": "0.42",
		"unit": "kWh",
		"region": "Tunisia",
		"year": 2026,
	},
	{
		"name": "Electricity — France",
		"category": ActivityCategory.ELECTRICITY,
		"scope": Scope.SCOPE_2,
		"value": "0.06",
		"unit": "kWh",
		"region": "France",
		"year": 2026,
	},
	{
		"name": "Electricity — Global average",
		"category": ActivityCategory.ELECTRICITY,
		"scope": Scope.SCOPE_2,
		"value": "0.45",
		"unit": "kWh",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Natural gas — combustion",
		"category": ActivityCategory.NATURAL_GAS,
		"scope": Scope.SCOPE_1,
		"value": "2.02",
		"unit": "m3",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Diesel — combustion",
		"category": ActivityCategory.FUEL,
		"scope": Scope.SCOPE_1,
		"value": "2.68",
		"unit": "L",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Petrol (gasoline) — combustion",
		"category": ActivityCategory.FUEL,
		"scope": Scope.SCOPE_1,
		"value": "2.31",
		"unit": "L",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Air travel — short haul",
		"category": ActivityCategory.BUSINESS_TRAVEL,
		"scope": Scope.SCOPE_3,
		"value": "0.18",
		"unit": "passenger-km",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Car travel — average passenger car",
		"category": ActivityCategory.TRANSPORTATION,
		"scope": Scope.SCOPE_3,
		"value": "0.17",
		"unit": "km",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Purchased goods — average",
		"category": ActivityCategory.PURCHASED_GOODS,
		"scope": Scope.SCOPE_3,
		"value": "1.00",
		"unit": "kg",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Waste — mixed landfill",
		"category": ActivityCategory.WASTE,
		"scope": Scope.SCOPE_3,
		"value": "0.55",
		"unit": "kg",
		"region": "Global",
		"year": 2026,
	},
	{
		"name": "Water supply and treatment",
		"category": ActivityCategory.WATER,
		"scope": Scope.SCOPE_3,
		"value": "0.35",
		"unit": "m3",
		"region": "Tunisia",
		"year": 2026,
	},
]


class Command(BaseCommand):
	help = "Seed the demo emission factor library (idempotent, flagged as DEMO)."

	def handle(self, *args, **options):
		created = updated = 0
		for payload in DEMO_FACTORS:
			_, was_created = EmissionFactor.objects.update_or_create(
				name=payload["name"],
				category=payload["category"],
				region=payload["region"],
				year=payload["year"],
				defaults={
					"scope": payload["scope"],
					"value": payload["value"],
					"unit": payload["unit"],
					"source": DEMO_SOURCE,
					"methodology": DEMO_METHODOLOGY,
					"verification_status": VerificationStatus.DEMO,
					"is_active": True,
				},
			)
			created += int(was_created)
			updated += int(not was_created)
		self.stdout.write(self.style.SUCCESS(
			f"Demo emission factors ready: {created} created, {updated} updated. "
			"These are illustrative values, not official emission factors."
		))
