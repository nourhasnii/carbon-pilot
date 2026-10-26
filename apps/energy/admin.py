from django.contrib import admin

from .models import EnergyAsset, EnergyReading


@admin.register(EnergyAsset)
class EnergyAssetAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "company", "asset_type", "energy_type", "is_active", "is_demo")
    list_filter = ("company", "asset_type", "energy_type", "is_demo")
    search_fields = ("name", "code")


@admin.register(EnergyReading)
class EnergyReadingAdmin(admin.ModelAdmin):
    list_display = ("asset", "timestamp", "value", "unit", "is_demo")
    list_filter = ("asset__company", "unit", "is_demo")
    date_hierarchy = "timestamp"


from .models import OptimizationScenario


@admin.register(OptimizationScenario)
class OptimizationScenarioAdmin(admin.ModelAdmin):
    list_display = ("name", "asset", "action", "reduction_percent", "saved_kwh", "is_demo")
    list_filter = ("asset__company", "action", "is_demo")


from .models import EnergyAnomaly


@admin.register(EnergyAnomaly)
class EnergyAnomalyAdmin(admin.ModelAdmin):
    list_display = ("asset", "date", "kind", "severity", "status", "is_demo")
    list_filter = ("asset__company", "kind", "severity", "status", "is_demo")
