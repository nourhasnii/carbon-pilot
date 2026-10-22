from django.contrib import admin
from .models import Company, Department, UserProfile

admin.site.register(Company)
admin.site.register(Department)
admin.site.register(UserProfile)
