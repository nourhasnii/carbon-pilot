"""Rôles (Django Groups) et permissions du module Energy.

Super Admin : géré via is_superuser (aucune permission de groupe nécessaire).
"""
ROLE_SUSTAINABILITY_MANAGER = "Admin RSE"
ROLE_DEPT_MANAGER = "Manager de département"
ROLE_EMPLOYEE = "Collaborateur"
ROLE_AUDITOR = "Auditeur externe"

ENERGY_FULL = ["view", "add", "change", "delete"]

# rôle -> {modèle: [actions]}
ENERGY_ROLE_PERMISSIONS = {
    ROLE_SUSTAINABILITY_MANAGER: {
        "energyasset": ENERGY_FULL,
        "energyreading": ENERGY_FULL,
        "importbatch": ["view", "add"],
        "energyforecast": ENERGY_FULL,
        "optimizationscenario": ENERGY_FULL,
        "energyanomaly": ["view", "add", "change"],
    },
    ROLE_DEPT_MANAGER: {
        "energyasset": ["view"],
        "energyreading": ["view", "add"],
        "importbatch": ["view", "add"],
        "energyforecast": ["view", "add"],
        "optimizationscenario": ["view", "add", "change"],
        "energyanomaly": ["view", "add", "change"],
    },
    ROLE_EMPLOYEE: {"energyasset": ["view"], "energyreading": ["view"], "energyforecast": ["view"]},
    ROLE_AUDITOR: {
        "energyasset": ["view"],
        "energyreading": ["view"],
        "importbatch": ["view"],
        "energyforecast": ["view"],
        "optimizationscenario": ["view"],
        "energyanomaly": ["view"],
    },
}
