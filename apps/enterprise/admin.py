from django.contrib import admin

from .models import Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
	list_display = ("name", "owner", "created_at", "updated_at")
	search_fields = ("name", "owner__email")
