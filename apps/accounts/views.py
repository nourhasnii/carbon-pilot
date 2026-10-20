from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST, require_http_methods

from .forms import CreateUserForm, SignupForm, UserLoginForm
from .models import AdminActivity, User
from .permissions import role_required
from .services.account_workflow import (
    record_admin_activity,
    resend_approval_email,
    transition_account_status,
)
from .services.email_service import build_account_activation_url, send_company_invitation_email
from .services import notifications as notification_service
from apps.carbon.services import analytics as carbon_analytics
from apps.carbon.services.tenancy import company_for_user, visible_activities
from apps.enterprise.forms import CompanyForm, CompanyInviteForm, DepartmentForm
from apps.enterprise.models import Company, Department


DASHBOARD_ROUTES = {
    role: "accounts:dashboard" for role, _ in User.Role.choices
}
ROLE_SIDEBAR = {
    User.Role.SUPER_ADMIN: (
        {"label": "Dashboard", "icon": "layout-dashboard", "url_name": "accounts:dashboard", "key": "overview"},
        {"label": "Users", "icon": "users", "url_name": "accounts:user_management"},
        {"label": "Companies", "icon": "building-2", "key": "companies", "href": "/dashboard/?section=companies"},
        {"label": "ESG", "icon": "chart-no-axes-column-increasing", "url_name": "accounts:esg_reports"},
        {"label": "Audits", "icon": "clipboard-check", "url_name": "accounts:auditor_audit_risk"},
        {"label": "Suppliers", "icon": "truck", "url_name": "accounts:supplier_information"},
        {"label": "Platform / Settings", "icon": "settings-2", "note": "Soon", "unavailable": True},
    ),
    User.Role.COMPANY_OWNER: (
        {"label": "Dashboard", "icon": "layout-dashboard", "url_name": "accounts:dashboard", "key": "overview"},
        {"label": "My Company", "icon": "building-2", "key": "company", "href": "/dashboard/?section=company"},
        {"label": "Users", "icon": "users", "key": "users", "href": "/dashboard/?section=users", "requires_company": True},
        {"label": "Departments", "icon": "network", "key": "departments", "href": "/dashboard/?section=departments", "requires_company": True},
        {"label": "Carbon Management", "icon": "leaf", "url_name": "carbon:dashboard", "requires_company": True},
        {"label": "ESG", "icon": "chart-no-axes-column-increasing", "note": "Soon", "unavailable": True},
        {"label": "Audits", "icon": "clipboard-check", "note": "Soon", "unavailable": True},
        {"label": "Suppliers", "icon": "truck", "note": "Soon", "unavailable": True},
        {"label": "Profile", "icon": "user-round", "note": "Soon", "unavailable": True},
        {"label": "Settings", "icon": "settings-2", "note": "Soon", "unavailable": True},
    ),
    User.Role.DEPARTMENT_MANAGER: (
        {"label": "Dashboard", "icon": "layout-dashboard", "url_name": "accounts:dashboard", "key": "overview"},
        {"label": "Activities", "icon": "activity", "url_name": "carbon:activity_list"},
        {"label": "Review queue", "icon": "clipboard-check", "url_name": "carbon:review_queue"},
        {"label": "Emissions", "icon": "cloud", "url_name": "carbon:footprint"},
        {"label": "Objectives", "icon": "target", "url_name": "objectives:objective_list"},
        {"label": "Profile", "icon": "user-round", "url_name": "accounts:profile"},
    ),
    User.Role.SUPPLIER: (
        {"label": "Dashboard", "icon": "layout-dashboard", "url_name": "accounts:dashboard", "key": "overview"},
        {"label": "My information", "icon": "contact-round", "url_name": "accounts:supplier_information"},
        {"label": "Emissions", "icon": "cloud", "url_name": "accounts:supplier_emissions"},
    ),
    User.Role.AUDITOR: (
        {"label": "Dashboard", "icon": "layout-dashboard", "url_name": "accounts:dashboard", "key": "overview"},
        {"label": "Reports", "icon": "file-chart-column-increasing", "url_name": "accounts:auditor_reports"},
        {"label": "Audits", "icon": "clipboard-check", "url_name": "accounts:auditor_audit_risk"},
    ),
    User.Role.ESG_REPORTING_MANAGER: (
        {"label": "Dashboard", "icon": "layout-dashboard", "url_name": "accounts:dashboard", "key": "overview"},
        {"label": "ESG reports", "icon": "file-chart-column-increasing", "url_name": "accounts:esg_reports"},
        {"label": "Indicators", "icon": "chart-no-axes-combined", "url_name": "accounts:esg_indicators"},
        {"label": "Carbon", "icon": "leaf", "url_name": "carbon:dashboard"},
    ),
}


def landing_page(request):
    return render(request, "landing.html")


def login_page(request):
    form = UserLoginForm(request=request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        login(request, user)
        if not request.POST.get("remember"):
            request.session.set_expiry(0)
        if user.must_change_password:
            return redirect("accounts:force_password_change")
        next_url = request.POST.get("next") or request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect(DASHBOARD_ROUTES[user.role])
    return render(request, "login.html", {"form": form, "next": request.GET.get("next", "")})


def signup_page(request):
    form = SignupForm(data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your account request was submitted and is awaiting administrator approval.")
        return redirect("accounts:login")
    return render(request, "signup.html", {"form": form})


@require_POST
@login_required
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


@login_required
def dashboard_view(request):
    if request.user.status != User.Status.ACTIVE:
        logout(request)
        return redirect("accounts:login")

    role = request.user.role
    company = None
    company_form = None
    company_section = role == User.Role.COMPANY_OWNER and request.GET.get("section") == "company"
    department_section = role == User.Role.COMPANY_OWNER and request.GET.get("section") == "departments"
    users_section = role == User.Role.COMPANY_OWNER and request.GET.get("section") == "users"
    companies_section = role == User.Role.SUPER_ADMIN and request.GET.get("section") == "companies"
    department_form = None
    invite_form = None
    company_departments = Department.objects.none()
    company_admin_detail = None
    company_admin_users = User.objects.none()
    companies = Company.objects.none()
    company_search = ""
    company_status_filter = ""
    sidebar_links = [dict(item) for item in ROLE_SIDEBAR.get(role, ROLE_SIDEBAR[User.Role.SUPPLIER])]
    active_key = "overview"
    if role == User.Role.SUPER_ADMIN and companies_section:
        active_key = "companies"
    elif role == User.Role.COMPANY_OWNER:
        if users_section:
            active_key = "users"
        elif department_section:
            active_key = "departments"
        elif company_section:
            active_key = "company"
    for item in sidebar_links:
        item["active"] = item.get("key") == active_key
    if role == User.Role.SUPER_ADMIN and companies_section:
        action = request.POST.get("action") if request.method == "POST" else None
        if action in ("activate_company", "deactivate_company"):
            if request.POST.get("confirm_company_status") != "yes":
                raise PermissionDenied
            target_company = get_object_or_404(Company, pk=request.POST.get("company_id"))
            target_company.is_active = action == "activate_company"
            target_company.save(update_fields=("is_active", "updated_at"))
            status_label = "activated" if target_company.is_active else "deactivated"
            messages.success(request, f"{target_company.name} was {status_label}.")
            return redirect(
                f"{reverse('accounts:dashboard')}?section=companies&company={target_company.pk}"
            )

        company_search = request.GET.get("q", "").strip()
        company_status_filter = request.GET.get("status", "")
        companies = Company.objects.annotate(user_count=Count("members")).select_related("owner")
        if company_search:
            companies = companies.filter(name__icontains=company_search)
        if company_status_filter == "active":
            companies = companies.filter(is_active=True)
        elif company_status_filter == "deactivated":
            companies = companies.filter(is_active=False)
        company_id = request.GET.get("company")
        if company_id:
            company_admin_detail = get_object_or_404(Company, pk=company_id)
            company_admin_users = company_admin_detail.members.exclude(
                role=User.Role.SUPER_ADMIN
            ).order_by("last_name", "first_name", "email")
    if role == User.Role.COMPANY_OWNER:
        company = Company.objects.filter(owner=request.user).first()
        if company:
            company_departments = Department.objects.filter(company=company)
        if (
            company_section
            and company
            and request.method == "POST"
            and request.POST.get("action") == "deactivate_company"
        ):
            if request.POST.get("confirm_deactivation") != "yes":
                raise PermissionDenied
            company.is_active = False
            company.save(update_fields=("is_active", "updated_at"))
            logout(request)
            messages.success(request, "Your company has been deactivated. Contact a Super Admin to reactivate it.")
            return redirect("accounts:login")
        if users_section and company and request.method == "POST" and request.POST.get("action") in (
            "deactivate_user", "reactivate_user"
        ):
            target_user = get_object_or_404(
                User,
                pk=request.POST.get("user_id"),
                company_membership=company,
            )
            if target_user.pk == request.user.pk or target_user.role == User.Role.SUPER_ADMIN:
                raise PermissionDenied
            new_status = {
                "deactivate_user": User.Status.INACTIVE,
                "reactivate_user": User.Status.ACTIVE,
            }[request.POST["action"]]
            allowed_status = (
                target_user.status == User.Status.ACTIVE
                if new_status == User.Status.INACTIVE
                else target_user.status == User.Status.INACTIVE
            )
            if not allowed_status:
                messages.error(request, "That action is not valid for this account status.")
            else:
                target_user.status = new_status
                target_user.save(update_fields=("status", "is_active", "updated_at"))
                messages.success(request, f"{target_user.email} is now {target_user.get_status_display().lower()}.")
            return redirect(f"{reverse('accounts:dashboard')}?section=users")
        if (
            users_section
            and company
            and request.method == "POST"
            and request.POST.get("action") == "resend_invitation"
        ):
            invited_user = get_object_or_404(
                User,
                pk=request.POST.get("user_id"),
                company=company.name,
                status=User.Status.INVITED,
            )
            activation_url = build_account_activation_url(invited_user)
            email_sent = send_company_invitation_email(invited_user, activation_url)
            if email_sent:
                messages.success(request, f"Invitation resent to {invited_user.email}.")
            else:
                messages.warning(request, f"The invitation email could not be sent to {invited_user.email}.")
            return redirect(f"{reverse('accounts:dashboard')}?section=users")
        if users_section and company and (
            request.method == "POST" or request.GET.get("action") == "invite"
        ):
            invite_form = CompanyInviteForm(
                company,
                data=request.POST or None,
            )
            if request.method == "POST" and invite_form.is_valid():
                invited_user = invite_form.save()
                activation_url = build_account_activation_url(invited_user)
                email_sent = send_company_invitation_email(invited_user, activation_url)
                if email_sent:
                    messages.success(request, f"Invitation sent to {invited_user.email}.")
                else:
                    messages.warning(request, f"The invitation email could not be sent to {invited_user.email}.")
                return redirect(f"{reverse('accounts:dashboard')}?section=users")
        if department_section and company:
            editing_id = request.GET.get("edit")
            department = get_object_or_404(Department, company=company, pk=editing_id) if editing_id else None
            if request.method == "POST" or request.GET.get("action") == "create" or editing_id:
                department_form = DepartmentForm(request.POST or None, instance=department)
            if request.method == "POST" and department_form.is_valid():
                department = department_form.save(commit=False)
                department.company = company
                department.save()
                messages.success(request, "Department saved.")
                return redirect(f"{reverse('accounts:dashboard')}?section=departments")
        if company_section and (request.method == "POST" or company is None or request.GET.get("edit") == "1"):
            company_form = CompanyForm(
                request.POST or None,
                instance=company,
                initial={"name": request.user.company} if company is None else None,
            )
            if request.method == "POST" and company_form.is_valid():
                company = company_form.save(commit=False)
                company.owner = request.user
                company.save()
                if Company.objects.filter(name__iexact=company.name).count() == 1:
                    User.objects.filter(
                        company__iexact=company.name,
                        company_membership__isnull=True,
                    ).exclude(pk=request.user.pk).update(company_membership=company)
                User.objects.filter(company_membership=company).update(company=company.name)
                if request.user.company != company.name:
                    request.user.company = company.name
                request.user.company_membership = company
                request.user.save(update_fields=("company", "company_membership", "updated_at"))
                messages.success(request, "Company profile saved.")
                return redirect(f"{reverse('accounts:dashboard')}?section=company")
        for item in sidebar_links:
            if item.get("requires_company") and company is None:
                item["unavailable"] = True
                item["note"] = "Set up"

    context = {
        "dashboard_title": "Platform overview" if role == User.Role.SUPER_ADMIN else "Workspace overview",
        "sidebar_group": {
            User.Role.SUPER_ADMIN: "Platform",
            User.Role.COMPANY_OWNER: "Company",
            User.Role.DEPARTMENT_MANAGER: "Department",
            User.Role.SUPPLIER: "My workspace",
            User.Role.AUDITOR: "Assurance",
            User.Role.ESG_REPORTING_MANAGER: "Sustainability",
        }.get(role, "Workspace"),
        "sidebar_links": sidebar_links,
        "role_label": request.user.get_role_display(),
        "workspace_actions": [item for item in sidebar_links if item["label"] != "Dashboard"],
        "tenant_name": (
            "CarbonPilot Platform"
            if role == User.Role.SUPER_ADMIN
            else (company.name if company else (request.user.company or "No company linked"))
        ),
        "company": company,
        "company_section": company_section,
        "company_form": company_form,
        "department_section": department_section,
        "department_form": department_form,
        "company_departments": company_departments,
        "users_section": users_section,
        "invite_form": invite_form,
        "companies_section": companies_section,
        "companies": companies,
        "company_admin_detail": company_admin_detail,
        "company_admin_users": company_admin_users,
        "company_search": company_search,
        "company_status_filter": company_status_filter,
    }

    if role == User.Role.SUPER_ADMIN:
        user_counts = User.objects.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=User.Status.PENDING)),
            active=Count("id", filter=Q(status=User.Status.ACTIVE)),
        )
        company_count = User.objects.exclude(company="").values("company").distinct().count()
        role_counts = dict(
            User.objects.values_list("role")
            .annotate(total=Count("id"))
            .values_list("role", "total")
        )
        distribution = [
            {"label": label, "count": role_counts.get(role_name, 0)}
            for role_name, label in User.Role.choices
        ]
        largest_role_count = max((item["count"] for item in distribution), default=0)
        for item in distribution:
            item["percent"] = round(item["count"] * 100 / largest_role_count) if largest_role_count else 0

        context.update({
            "stats": {**user_counts, "companies": company_count},
            "distribution": distribution,
            "users": User.objects.exclude(role=User.Role.SUPER_ADMIN).order_by("-created_at")[:8],
            "pending_users": User.objects.filter(status=User.Status.PENDING)
            .exclude(role=User.Role.SUPER_ADMIN)
            .order_by("created_at")[:8],
            "recent_activity": AdminActivity.objects.select_related("actor", "target_user")[:8],
            "role_choices": [
                (role_name, label) for role_name, label in User.Role.choices
                if role_name != User.Role.SUPER_ADMIN
            ],
        })
    elif role == User.Role.COMPANY_OWNER:
        company_users = User.objects.none()
        departments = User.objects.none()
        if company:
            company_users = User.objects.filter(company_membership=company).exclude(
                role=User.Role.SUPER_ADMIN
            )
            departments = (
                company_users.exclude(department="")
                .values("department")
                .annotate(member_count=Count("id"))
                .order_by("department")
            )
        context.update({
            "company_users": company_users.order_by("last_name", "first_name", "email"),
            "active_member_count": company_users.exclude(pk=request.user.pk)
            .filter(status=User.Status.ACTIVE)
            .count(),
            "departments": departments,
            "department_count": departments.count(),
        })
        if company:
            context["carbon_summary"] = _company_carbon_summary(request.user, company)
    elif role == User.Role.DEPARTMENT_MANAGER:
        context["workspace_intro"] = (
            "Review your department's submissions and keep its carbon data audit-ready."
        )
        dm_company = company_for_user(request.user)
        if dm_company is not None:
            context["carbon_summary"] = _company_carbon_summary(request.user, dm_company)
    else:
        context["workspace_intro"] = f"Your {request.user.get_role_display()} workspace and permitted tools."

    if role == User.Role.SUPER_ADMIN:
        if companies_section:
            if company_admin_detail:
                context["page_eyebrow"] = "Platform company"
                context["page_title"] = company_admin_detail.name
                context["page_subtitle"] = "Company profile, status and members."
            else:
                context["page_eyebrow"] = "Platform companies"
                context["page_title"] = "Companies"
                context["page_subtitle"] = "Every registered company workspace on CarbonPilot."
        else:
            context["page_eyebrow"] = "Platform administration"
            context["page_title"] = "Dashboard"
            context["page_subtitle"] = "Accounts, access requests and platform activity."
    elif role == User.Role.COMPANY_OWNER:
        if users_section:
            context["page_eyebrow"] = "Company workspace"
            context["page_title"] = "Invite user" if invite_form else "Users"
            context["page_subtitle"] = "Manage who can access your company workspace."
        elif department_section:
            context["page_eyebrow"] = "Company workspace"
            context["page_title"] = "Departments"
            context["page_subtitle"] = "Organize your company into departments."
        elif company_section:
            context["page_eyebrow"] = "Company profile"
            context["page_title"] = "My Company"
            context["page_subtitle"] = "Your organization identity and sustainability profile."
        else:
            context["page_eyebrow"] = "Company workspace"
            context["page_title"] = "Dashboard"
            context["page_subtitle"] = "Your carbon footprint, team and company at a glance."
    else:
        context["page_eyebrow"] = context["sidebar_group"]
        context["page_title"] = "Dashboard"
        context["page_subtitle"] = (
            f"{request.user.get_role_display()} workspace and permitted tools."
        )

    return render(request, "accounts/admin_dashboard.html", context)


def _company_carbon_summary(user, company):
    """Carbon/ESG KPIs for a company dashboard, scoped to the user's tenant.

    Reuses the carbon module's analytics services so the figures stay consistent
    with the Carbon Management workspace. Approved activities only.
    """
    visible = visible_activities(user, company)
    approved = visible.approved()
    today = timezone.localdate()
    years = carbon_analytics.available_years(approved, today.year)
    year = years[0]
    scoped = approved.filter(period_start__year=year)
    return {
        "year": year,
        "years": years,
        "total_tonnes": carbon_analytics.total_tonnes(scoped),
        "scopes": carbon_analytics.scope_breakdown(scoped),
        "comparison": carbon_analytics.year_comparison(approved, year),
        "quality": carbon_analytics.quality_summary(scoped),
        "open_review": carbon_analytics.open_review_count(visible),
        "approved_count": scoped.count(),
        "recent": carbon_analytics.recent(visible, limit=5),
    }


@login_required
@role_required(User.Role.SUPER_ADMIN)
def user_management(request):
    users = User.objects.exclude(role=User.Role.SUPER_ADMIN)
    role_choices = [
        (role, label) for role, label in User.Role.choices
        if role != User.Role.SUPER_ADMIN
    ]
    return render(request, "accounts/user_management.html", {
        "users": users,
        "role_choices": role_choices,
    })


@login_required
@role_required(User.Role.SUPER_ADMIN)
def create_user(request):
    form = CreateUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        record_admin_activity(request.user, AdminActivity.Action.CREATE_ACCOUNT, user)
        messages.success(request, f"{user.get_full_name() or user.email} was created with pending status.")
        return redirect("accounts:user_management")
    return render(request, "accounts/user_create.html", {"form": form})


@require_POST
@login_required
@role_required(User.Role.SUPER_ADMIN)
def manage_user_action(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    if user.role == User.Role.SUPER_ADMIN:
        raise PermissionDenied
    action_status = {
        "accept": User.Status.ACTIVE,
        "reject": User.Status.REJECTED,
        "deactivate": User.Status.INACTIVE,
        "reactivate": User.Status.ACTIVE,
    }
    action = request.POST.get("action")
    status = action_status.get(action)
    if action == "resend_approval":
        changed, email_sent = resend_approval_email(user)
        activity = AdminActivity.Action.RESEND_APPROVAL
    elif status:
        assigned_role = request.POST.get("role") if action == "accept" else None
        changed, email_sent = (
            (False, None)
            if action == "accept" and not assigned_role
            else transition_account_status(user, status, assigned_role)
        )
        activity = {
            "accept": AdminActivity.Action.APPROVE_ACCOUNT,
            "reject": AdminActivity.Action.REJECT_ACCOUNT,
            "deactivate": AdminActivity.Action.DEACTIVATE_ACCOUNT,
            "reactivate": AdminActivity.Action.REACTIVATE_ACCOUNT,
        }[action]
    else:
        changed, email_sent = False, None
    if not changed:
        messages.error(request, "That action is not valid for this account status.")
    else:
        record_admin_activity(request.user, activity, user)
        if email_sent is False:
            messages.warning(
                request,
                "The account was updated, but the email could not be sent. "
                "Retry the approval email from user management.",
            )
        else:
            messages.success(request, f"{user.email} is now {user.get_status_display().lower()}.")
    return redirect("accounts:user_management")


@require_http_methods(["GET", "POST"])
def set_password(request, uidb64, token):
    try:
        user_id = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError, User.DoesNotExist):
        user = None

    if (
        user is None
        or user.status not in (User.Status.ACTIVE, User.Status.INVITED)
        or not default_token_generator.check_token(user, token)
    ):
        return render(request, "accounts/set_password.html", {"invalid_link": True}, status=400)

    invited = user.status == User.Status.INVITED
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        user.must_change_password = False
        if invited:
            user.status = User.Status.ACTIVE
            user.save(update_fields=("status", "is_active", "must_change_password", "updated_at"))
            messages.success(request, "Your account is active. Sign in with your new password.")
            return redirect("accounts:login")
        user.save(update_fields=("must_change_password", "updated_at"))
        login(request, user, backend="apps.accounts.backends.CompanyAccessModelBackend")
        messages.success(request, "Your account is active. Welcome to CarbonPilot.")
        return redirect(DASHBOARD_ROUTES[user.role])
    return render(request, "accounts/set_password.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def force_password_change(request):
    if not request.user.must_change_password:
        return redirect(DASHBOARD_ROUTES.get(request.user.role, "accounts:login"))

    form = SetPasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.save(update_fields=("must_change_password", "updated_at"))
        update_session_auth_hash(request, user)
        messages.success(request, "Your password has been changed.")
        return redirect(DASHBOARD_ROUTES.get(user.role, "accounts:login"))
    return render(request, "accounts/set_password.html", {"form": form, "forced": True})


@login_required
def profile_view(request):
    """Account profile: identity plus the tenant/department this user belongs to."""
    sidebar = [dict(item) for item in ROLE_SIDEBAR.get(request.user.role, ())]
    for item in sidebar:
        item["active"] = item.get("url_name") == "accounts:profile"
    return render(request, "accounts/profile.html", {
        "company": company_for_user(request.user),
        "sidebar_nav": sidebar,
    })


@require_POST
@login_required
def notifications_mark_read(request):
    """Mark every in-app notification of the current user as read."""
    notification_service.mark_all_read(request.user)
    next_url = request.POST.get("next")
    if next_url and next_url.startswith("/") and "//" not in next_url:
        return redirect(next_url)
    return redirect("accounts:dashboard")


@login_required
@role_required(User.Role.SUPPLIER)
def supplier_information(request):
    return render(request, "role_dashboard.html", {"dashboard_title": "Supplier information"})


@login_required
@role_required(User.Role.SUPPLIER)
def supplier_emissions(request):
    return render(request, "role_dashboard.html", {"dashboard_title": "Supplier emissions"})


@login_required
@role_required(User.Role.AUDITOR)
def auditor_reports(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return render(request, "role_dashboard.html", {"dashboard_title": "ESG reports"})


@login_required
@role_required(User.Role.AUDITOR)
def auditor_audit_risk(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return render(request, "role_dashboard.html", {"dashboard_title": "Audit and risk"})


@require_http_methods(["GET", "POST"])
@login_required
@role_required(User.Role.ESG_REPORTING_MANAGER)
def esg_reports(request):
    if request.method == "POST":
        messages.success(request, "Report generation is available when the reporting module is connected.")
    return render(request, "role_dashboard.html", {"dashboard_title": "ESG reports"})


@login_required
@role_required(User.Role.ESG_REPORTING_MANAGER)
def esg_indicators(request):
    return render(request, "role_dashboard.html", {"dashboard_title": "ESG indicators"})