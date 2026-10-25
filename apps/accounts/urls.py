from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('', views.landing_page, name='landing'),
    path('login/', views.login_page, name='login'),
    path('signup/', views.signup_page, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('account/change-password/', views.force_password_change, name='force_password_change'),
    path('account/profile/', views.profile_view, name='profile'),
    path('notifications/mark-read/', views.notifications_mark_read, name='notifications_mark_read'),
    path('dashboard/admin/', views.dashboard_view, name='admin_dashboard'),
    path('dashboard/department/', views.dashboard_view, name='department_dashboard'),
    path('dashboard/supplier/', views.dashboard_view, name='supplier_dashboard'),
    path('dashboard/auditor/', views.dashboard_view, name='auditor_dashboard'),
    path('dashboard/esg/', views.dashboard_view, name='esg_dashboard'),
    path('admin/users/', views.user_management, name='user_management'),
    path('admin/users/create/', views.create_user, name='create_user'),
    path('admin/users/<int:user_id>/action/', views.manage_user_action, name='manage_user_action'),
    path('accounts/activate/<uidb64>/<token>/', views.set_password, name='set_password'),
    path('account/activate/<uidb64>/<token>/', views.set_password, name='legacy_set_password'),
    path('supplier/information/', views.supplier_information, name='supplier_information'),
    path('supplier/emissions/', views.supplier_emissions, name='supplier_emissions'),
    path('auditor/reports/', views.auditor_reports, name='auditor_reports'),
    path('auditor/audit-risk/', views.auditor_audit_risk, name='auditor_audit_risk'),
    path('esg/reports/', views.esg_reports, name='esg_reports'),
    path('esg/indicators/', views.esg_indicators, name='esg_indicators'),
]
