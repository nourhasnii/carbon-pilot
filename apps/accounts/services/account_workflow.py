import secrets

from .email_service import (
    build_account_activation_url,
    send_account_approval_email,
    send_account_activation_email,
    send_account_rejection_email,
)
from ..models import AdminActivity, User


ALLOWED_TRANSITIONS = {
    User.Status.PENDING: {User.Status.ACTIVE, User.Status.REJECTED},
    User.Status.ACTIVE: {User.Status.INACTIVE},
    User.Status.INACTIVE: {User.Status.ACTIVE},
    User.Status.REJECTED: set(),
}


def record_admin_activity(actor, action, target_user):
    return AdminActivity.objects.create(
        actor=actor,
        action=action,
        target_user=target_user,
        target_name=target_user.get_full_name(),
        target_email=target_user.email,
    )


def transition_account_status(user, new_status, assigned_role=None):
    if new_status not in ALLOWED_TRANSITIONS.get(user.status, set()):
        return False, None

    needs_initial_credentials = (
        user.status == User.Status.PENDING and new_status == User.Status.ACTIVE
    )
    if assigned_role is not None:
        allowed_roles = {value for value, _label in User.Role.choices}
        allowed_roles.discard(User.Role.SUPER_ADMIN)
        if not needs_initial_credentials or assigned_role not in allowed_roles:
            return False, None
        user.role = assigned_role

    user.status = new_status
    update_fields = ["status", "is_active", "updated_at"]
    if needs_initial_credentials:
        temporary_password = secrets.token_urlsafe(24)
        user.set_password(temporary_password)
        user.must_change_password = True
        update_fields.extend(("role", "password", "must_change_password"))
    user.save(update_fields=update_fields)
    if needs_initial_credentials:
        return True, send_account_approval_email(user, temporary_password)
    if new_status == User.Status.ACTIVE:
        activation_url = build_account_activation_url(user)
        return True, send_account_activation_email(user, activation_url)
    if new_status == User.Status.REJECTED:
        return True, send_account_rejection_email(user)
    return True, None


def resend_approval_email(user):
    if user.status != User.Status.ACTIVE or not user.must_change_password:
        return False, None

    temporary_password = secrets.token_urlsafe(24)
    user.set_password(temporary_password)
    user.save(update_fields=("password", "updated_at"))
    return True, send_account_approval_email(user, temporary_password)