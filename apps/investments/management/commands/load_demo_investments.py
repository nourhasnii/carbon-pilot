"""Charge un jeu de projets d'investissement explicitement marqué DEMO."""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.enterprise.models import Company

from ...models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectStatus,
	ProjectType,
)


DEMO_PROJECTS = (
	{
		"name": "[DEMO] Centrale solaire du site industriel",
		"type": ProjectType.SOLAR,
		"status": ProjectStatus.COMPLETED,
		"budget": Decimal("380000"),
		"actual_cost": Decimal("365000"),
		"tco2e": Decimal("285"),
		"actual_tco2e": Decimal("276"),
		"lifetime": 25,
		"savings": Decimal("72000"),
	},
	{
		"name": "[DEMO] Optimisation CVC du siège",
		"type": ProjectType.HVAC,
		"status": ProjectStatus.COMPLETED,
		"budget": Decimal("92000"),
		"actual_cost": Decimal("97800"),
		"tco2e": Decimal("42"),
		"actual_tco2e": Decimal("45"),
		"lifetime": 15,
		"savings": Decimal("18500"),
	},
	{
		"name": "[DEMO] Électrification de la flotte logistique",
		"type": ProjectType.FLEET_ELECTRIFICATION,
		"status": ProjectStatus.COMPLETED,
		"budget": Decimal("245000"),
		"actual_cost": Decimal("238500"),
		"tco2e": Decimal("198"),
		"actual_tco2e": Decimal("191"),
		"lifetime": 12,
		"savings": Decimal("54000"),
	},
	{
		"name": "[DEMO] Isolation de l'entrepôt nord",
		"type": ProjectType.INSULATION,
		"status": ProjectStatus.COMPLETED,
		"budget": Decimal("68000"),
		"actual_cost": Decimal("71500"),
		"tco2e": Decimal("18"),
		"actual_tco2e": Decimal("20"),
		"lifetime": 20,
		"savings": Decimal("11000"),
	},
	{
		"name": "[DEMO] Éclairage LED des ateliers",
		"type": ProjectType.LIGHTING,
		"status": ProjectStatus.COMPLETED,
		"budget": Decimal("31000"),
		"actual_cost": Decimal("29500"),
		"tco2e": Decimal("12"),
		"actual_tco2e": Decimal("11"),
		"lifetime": 10,
		"savings": Decimal("8200"),
	},
	{
		"name": "[DEMO] Ombrières solaires du parking",
		"type": ProjectType.SOLAR,
		"status": ProjectStatus.APPROVED,
		"budget": Decimal("210000"),
		"tco2e": Decimal("155"),
		"lifetime": 25,
		"savings": Decimal("41000"),
	},
	{
		"name": "[DEMO] Récupération de chaleur process",
		"type": ProjectType.HVAC,
		"status": ProjectStatus.APPROVED,
		"budget": Decimal("145000"),
		"tco2e": Decimal("96"),
		"lifetime": 18,
		"savings": Decimal("29500"),
	},
	{
		"name": "[DEMO] Bornes de recharge collaborateurs",
		"type": ProjectType.FLEET_ELECTRIFICATION,
		"status": ProjectStatus.APPROVED,
		"budget": Decimal("52000"),
		"tco2e": Decimal("8"),
		"lifetime": 12,
		"savings": Decimal("6500"),
	},
	{
		"name": "[DEMO] Vitrages performants du bâtiment administratif",
		"type": ProjectType.INSULATION,
		"status": ProjectStatus.PROPOSED,
		"budget": Decimal("185000"),
		"tco2e": Decimal("74"),
		"lifetime": 22,
		"savings": Decimal("26000"),
	},
	{
		"name": "[DEMO] Pilotage intelligent de l'éclairage",
		"type": ProjectType.LIGHTING,
		"status": ProjectStatus.PROPOSED,
		"budget": Decimal("24000"),
		"tco2e": Decimal("5"),
		"lifetime": 8,
		"savings": Decimal("4200"),
	},
	{
		"name": "[DEMO] Réduction des pertes de vapeur",
		"type": ProjectType.OTHER,
		"status": ProjectStatus.IN_PROGRESS,
		"budget": Decimal("118000"),
		"tco2e": Decimal("63"),
		"lifetime": 16,
		"savings": Decimal("21500"),
	},
	{
		"name": "[DEMO] Mobilité douce intersites",
		"type": ProjectType.OTHER,
		"status": ProjectStatus.CANCELLED,
		"budget": Decimal("27500"),
		"tco2e": Decimal("7"),
		"lifetime": 10,
		"savings": Decimal("3000"),
	},
)


class Command(BaseCommand):
	help = "Charge les projets d'investissement de démonstration d'une entreprise."

	def add_arguments(self, parser):
		parser.add_argument(
			"--company-id",
			type=int,
			required=True,
			help="Identifiant de l'entreprise cible.",
		)
		parser.add_argument(
			"--reset",
			action="store_true",
			help="Supprime les projets DEMO de l'entreprise avant de les recréer.",
		)

	def handle(self, *args, **options):
		try:
			company = Company.objects.get(pk=options["company_id"])
		except Company.DoesNotExist as exc:
			raise CommandError(
				f"L'entreprise avec l'identifiant {options['company_id']} n'existe pas."
			) from exc

		created = updated = 0
		with transaction.atomic():
			if options["reset"]:
				DecarbonizationProject.objects.filter(
					company=company,
					is_demo=True,
				).delete()

			for payload in DEMO_PROJECTS:
				project, was_created = DecarbonizationProject.objects.update_or_create(
					company=company,
					name=payload["name"],
					defaults={
						"project_type": payload["type"],
						"status": payload["status"],
						"description": "Projet fictif fourni pour illustrer le pilotage financier.",
						"start_date": date(2026, 1, 1),
						"end_date": date(2027, 12, 31),
						"is_demo": True,
					},
				)
				created += int(was_created)
				updated += int(not was_created)
				Investment.objects.update_or_create(
					project=project,
					defaults={
						"planned_budget": payload["budget"],
						"actual_cost": payload.get("actual_cost"),
						"committed_spend": payload.get(
							"actual_cost", payload["budget"]
						),
						"currency": "TND",
						"funding_source": "Budget de démonstration",
						"date": date(2026, 1, 15),
					},
				)
				ImpactAssessment.objects.update_or_create(
					project=project,
					defaults={
						"estimated_tco2e_per_year": payload["tco2e"],
						"estimated_kwh_saved_per_year": payload["tco2e"] * Decimal("1000"),
						"annual_savings": payload["savings"],
						"lifetime_years": payload["lifetime"],
						"assumptions": (
							"Estimation fictive fondée sur une consommation historique "
							"et des hypothèses de démonstration en TND."
						),
						"actual_tco2e_per_year": payload.get("actual_tco2e"),
					},
				)

		self.stdout.write(self.style.SUCCESS(
			f"Projets de démonstration prêts : {created} créés, {updated} mis à jour."
		))
