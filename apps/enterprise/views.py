from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.permissions import role_required

@login_required
@role_required(User.Role.COMPANY_OWNER)
def company_dashboard(request):
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


@login_required
@role_required(User.Role.COMPANY_OWNER)
def company_profile(request):
	return redirect(f"{reverse('accounts:dashboard')}?section=company")
