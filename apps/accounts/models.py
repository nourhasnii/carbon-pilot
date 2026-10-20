from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models


class UserManager(BaseUserManager):
	use_in_migrations = True

	def get_by_natural_key(self, username):
		return self.get(email__iexact=username)

	def create_user(self, email, password=None, **extra_fields):
		if not email:
			raise ValueError("An email address is required.")
		user = self.model(email=self.normalize_email(email).lower(), **extra_fields)
		if password:
			user.set_password(password)
		else:
			user.set_unusable_password()
		user.save(using=self._db)
		return user

	def create_superuser(self, email, password=None, **extra_fields):
		extra_fields["is_staff"] = True
		extra_fields["is_superuser"] = True
		extra_fields["role"] = User.Role.SUPER_ADMIN
		extra_fields["status"] = User.Status.ACTIVE
		if extra_fields.get("is_staff") is not True:
			raise ValueError("Superusers must have is_staff=True.")
		if extra_fields.get("is_superuser") is not True:
			raise ValueError("Superusers must have is_superuser=True.")
		return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
	class Role(models.TextChoices):
		SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
		COMPANY_OWNER = "COMPANY_OWNER", "Company Owner"
		DEPARTMENT_MANAGER = "DEPARTMENT_MANAGER", "Department Manager"
		SUPPLIER = "SUPPLIER", "Supplier"
		AUDITOR = "AUDITOR", "Auditor"
		ESG_REPORTING_MANAGER = "ESG_REPORTING_MANAGER", "ESG Reporting Manager"

	class Status(models.TextChoices):
		PENDING = "PENDING", "Pending"
		INVITED = "INVITED", "Invited"
		ACTIVE = "ACTIVE", "Active"
		REJECTED = "REJECTED", "Rejected"
		INACTIVE = "INACTIVE", "Inactive"

	username = None
	email = models.EmailField(unique=True)
	role = models.CharField(max_length=32, choices=Role.choices, default=Role.DEPARTMENT_MANAGER)
	status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
	must_change_password = models.BooleanField(default=False)
	company = models.CharField(max_length=255, blank=True)
	company_membership = models.ForeignKey(
		"enterprise.Company",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="members",
	)
	department = models.CharField(max_length=255, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	USERNAME_FIELD = "email"
	REQUIRED_FIELDS = []

	objects = UserManager()

	class Meta:
		ordering = ("last_name", "first_name", "email")

	def save(self, *args, **kwargs):
		self.email = self.email.strip().lower()
		self.is_active = self.status == self.Status.ACTIVE
		if self.role == self.Role.SUPER_ADMIN:
			self.status = self.Status.ACTIVE
			self.is_active = True
		super().save(*args, **kwargs)

	def __str__(self):
		return self.email


class AdminActivity(models.Model):
	class Action(models.TextChoices):
		CREATE_ACCOUNT = "CREATE_ACCOUNT", "Created account"
		APPROVE_ACCOUNT = "APPROVE_ACCOUNT", "Approved account"
		RESEND_APPROVAL = "RESEND_APPROVAL", "Resent approval credentials"
		REJECT_ACCOUNT = "REJECT_ACCOUNT", "Rejected account"
		DEACTIVATE_ACCOUNT = "DEACTIVATE_ACCOUNT", "Deactivated account"
		REACTIVATE_ACCOUNT = "REACTIVATE_ACCOUNT", "Reactivated account"

	actor = models.ForeignKey(
		"User", null=True, blank=True, on_delete=models.SET_NULL,
		related_name="admin_actions",
	)
	action = models.CharField(max_length=24, choices=Action.choices)
	target_user = models.ForeignKey(
		"User", null=True, blank=True, on_delete=models.SET_NULL,
		related_name="account_activity",
	)
	target_name = models.CharField(max_length=255, blank=True)
	target_email = models.EmailField()
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ("-created_at",)

	def __str__(self):
		return f"{self.get_action_display()}: {self.target_email}"


class Notification(models.Model):
	"""In-app notification delivered to a single user (never cross-tenant)."""

	class Kind(models.TextChoices):
		ACTIVITY_SUBMITTED = "ACTIVITY_SUBMITTED", "Activity submitted"
		ACTIVITY_APPROVED = "ACTIVITY_APPROVED", "Activity approved"
		ACTIVITY_REJECTED = "ACTIVITY_REJECTED", "Activity rejected"
		GENERIC = "GENERIC", "General"

	recipient = models.ForeignKey(
		"User", on_delete=models.CASCADE, related_name="notifications",
	)
	actor = models.ForeignKey(
		"User", null=True, blank=True, on_delete=models.SET_NULL,
		related_name="notifications_sent",
	)
	kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.GENERIC)
	title = models.CharField(max_length=255)
	message = models.TextField(blank=True)
	url = models.CharField(max_length=500, blank=True)
	is_read = models.BooleanField(default=False)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ("-created_at",)
		indexes = [
			models.Index(fields=("recipient", "is_read", "-created_at")),
		]

	def __str__(self):
		return f"{self.title} → {self.recipient_id}"
