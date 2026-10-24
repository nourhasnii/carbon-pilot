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

urlpatterns = [
    path('', include('apps.accounts.urls')),
    path('company/', include('apps.enterprise.urls')),
    path('carbon/', include('apps.carbon.urls')),
    path('objectives/', include('apps.objectives.urls')),
    path('investments/', include('apps.investments.urls')),
    path('portfolio-ai/', include('apps.portfolio_ai.urls')),
    path('recommendations/', include('apps.recommendations.urls')),
    path('admin/', admin.site.urls),
]
