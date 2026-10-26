from django.urls import path

from . import views

app_name = "suppliers"

urlpatterns = [
	path("", views.supplier_list, name="supplier_list"),
	path("new/", views.supplier_create, name="supplier_create"),
	path("compare/", views.supplier_compare, name="supplier_compare"),
	path("<int:pk>/", views.supplier_detail, name="supplier_detail"),
	path("<int:pk>/edit/", views.supplier_edit, name="supplier_edit"),
	path("<int:pk>/toggle/", views.supplier_toggle_active, name="supplier_toggle_active"),
	path("<int:pk>/documents/upload/", views.supplier_document_upload, name="supplier_document_upload"),
	path("documents/<int:pk>/download/", views.supplier_document_download, name="supplier_document_download"),
	path("documents/<int:pk>/delete/", views.supplier_document_delete, name="supplier_document_delete"),
]
