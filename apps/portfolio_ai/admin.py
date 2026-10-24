from django.contrib import admin

from .models import PortfolioRun


@admin.register(PortfolioRun)
class PortfolioRunAdmin(admin.ModelAdmin):
	list_display = ("company", "objective", "status", "budget", "created_by", "created_at")
	list_filter = ("objective", "status")
	search_fields = ("company__name", "created_by__email")
	readonly_fields = ("created_at",)
	list_select_related = ("company", "created_by")
