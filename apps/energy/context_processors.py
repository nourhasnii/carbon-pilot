from django.urls import reverse

from apps.carbon.views import _sidebar as carbon_sidebar

ENERGY_NS = "apps.energy"

# Sous-pages du module Energie : (libelle, icone Lucide, nom d'URL, prefixes des vues qui l'activent)
ENERGY_SUBPAGES = [
    ("Historique", "history", "history", ("history",)),
    ("Prévisions", "trending-up", "forecast_list", ("forecast_",)),
    ("Anomalies énergie", "triangle-alert", "anomaly_list", ("anomaly_",)),
    ("Scénarios", "flask-conical", "scenario_list", ("scenario_",)),
    ("Assets", "boxes", "asset_list", ("asset_",)),
    ("Mesures", "list", "reading_list", ("reading_",)),
    ("Import", "upload", "import", ("import",)),
]


def energy_sidebar(request):
    """Menu de l'equipe (sidebar_nav) pour les pages Energie, avec les sous-pages du module."""
    match = getattr(request, "resolver_match", None)
    if match is None or match.app_name != ENERGY_NS:
        return {}
    if not request.user.is_authenticated:
        return {}

    current = match.url_name or ""
    items = []
    for item in carbon_sidebar("Energy", request.user):
        item = dict(item)
        is_energy = str(item.get("url_name", "")).startswith(ENERGY_NS + ":")
        item["active"] = is_energy and current == "dashboard"
        items.append(item)
        if is_energy:
            for label, icon, name, prefixes in ENERGY_SUBPAGES:
                url_name = f"{ENERGY_NS}:{name}"
                items.append({
                    "label": label,
                    "icon": icon,
                    "url_name": url_name,
                    "href": reverse(url_name),
                    "active": current.startswith(prefixes),
                })
    return {"sidebar_nav": items}
