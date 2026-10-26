"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

# Project-wide error handlers. Declaring them explicitly means every module —
# the app URLconfs below as well as the admin — resolves `403.html`, `404.html`
# and `500.html` from the shared `templates/` directory.
#
# Development behaviour is unchanged: while ``DEBUG`` is True Django still shows
# its technical 404 / 500 debug pages, so tracebacks stay visible locally. These
# handlers (and the friendly templates) only take over when ``DEBUG`` is False,
# which is also the mode the test runner uses.
handler403 = "django.views.defaults.permission_denied"
handler404 = "django.views.defaults.page_not_found"
handler500 = "django.views.defaults.server_error"

urlpatterns = [
    path('', include('apps.accounts.urls')),
    path('company/', include('apps.enterprise.urls')),
    path('carbon/', include('apps.carbon.urls')),
    path('objectives/', include('apps.objectives.urls')),
    path('recommendations/', include('apps.recommendations.urls')),
    path('risk-assessment/', include('apps.risk.urls')),
    path('esg-reports/', include('apps.reports.urls')),
    path('suppliers/', include('apps.suppliers.urls')),
    path('assistant/', include('apps.assistant.urls')),
    path('governance/', include('apps.governance.urls')),
    path('audits/', include('apps.audits.urls')),
    path('esg/', include('apps.esg.urls')),
    path('admin/', admin.site.urls),
]
