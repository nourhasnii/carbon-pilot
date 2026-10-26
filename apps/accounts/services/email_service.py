import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.contrib.auth.tokens import default_token_generator
from django.utils.crypto import get_random_string


logger = logging.getLogger(__name__)
BREVO_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"


def build_account_activation_url(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse("accounts:set_password", kwargs={"uidb64": uid, "token": token})
    base_url = settings.APP_BASE_URL.rstrip("/") + "/"
    return urljoin(base_url, path.lstrip("/"))


def build_login_url():
    base_url = settings.APP_BASE_URL.rstrip("/") + "/"
    return urljoin(base_url, reverse("accounts:login").lstrip("/"))


def _safe_response_text(response_text, api_key):
    if api_key:
        response_text = response_text.replace(api_key, "[REDACTED]")
    return response_text[:2000]


def _send_transactional_email(user, subject, text_content, html_content):
    api_key = settings.BREVO_API_KEY
    sender_email = settings.DEFAULT_FROM_EMAIL
    sender_name = "CarbonPilot"
    if not api_key or not sender_email:
        logger.error(
            "[BREVO ERROR] Email not sent; required configuration missing "
            "(API key configured: %s, sender email configured: %s).",
            bool(api_key),
            bool(sender_email),
        )
        return False

    logger.info("[BREVO] Sending transactional email to: %s", user.email)
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": user.email, "name": user.get_full_name() or user.email}],
        "subject": subject,
        "textContent": text_content,
        "htmlContent": html_content,
    }
    request = Request(
        BREVO_EMAIL_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            response_text = response.read().decode("utf-8", errors="replace")
        logger.info("[BREVO] Brevo response status: %s", status)
        if 200 <= status < 300:
            logger.info("[BREVO] Transactional email sent successfully to: %s", user.email)
            return True
        logger.error(
            "[BREVO ERROR] Status: %s; Response: %s",
            status,
            _safe_response_text(response_text, api_key),
        )
    except HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        logger.error(
            "[BREVO ERROR] Status: %s; Response: %s",
            error.code,
            _safe_response_text(response_text, api_key),
        )
    except (URLError, TimeoutError, OSError) as error:
        logger.error(
            "[BREVO ERROR] Request failed (%s): %s",
            type(error).__name__,
            _safe_response_text(str(error), api_key),
        )
    except Exception as error:
        logger.error(
            "[BREVO ERROR] Unexpected request failure (%s): %s",
            type(error).__name__,
            _safe_response_text(str(error), api_key),
        )
    return False


def send_account_activation_email(user, activation_url):
    first_name = user.first_name or user.get_full_name() or user.email
    role = user.get_role_display()
    context = {
        "first_name": first_name,
        "role": role,
        "activation_url": activation_url,
    }
    html_content = render_to_string("emails/account_activation.html", context)
    text_content = (
        f"Hello {first_name},\n\n"
        "Your CarbonPilot account has been approved.\n\n"
        f"Role: {role}\n\n"
        "Your account is now ready to be activated. Create your password securely here:\n"
        f"{activation_url}\n\nRegards,\nCarbonPilot Team"
    )
    return _send_transactional_email(
        user,
        "Your CarbonPilot account has been approved",
        text_content,
        html_content,
    )


def send_company_invitation_email(user, activation_url):
    first_name = user.first_name or user.get_full_name() or user.email
    context = {
        "first_name": first_name,
        "email": user.email,
        "role": user.get_role_display(),
        "company": user.company,
        "department": user.department,
        "activation_url": activation_url,
    }
    html_content = render_to_string("emails/company_invitation.html", context)
    text_content = (
        f"Hello {first_name},\n\n"
        f"You have been invited to join {user.company} on CarbonPilot.\n\n"
        f"Role: {user.get_role_display()}\n"
        f"Department: {user.department or 'No department'}\n\n"
        "Set your password and activate your account using this secure link:\n"
        f"{activation_url}\n\nRegards,\nCarbonPilot Team"
    )
    if settings.DEBUG:
        logger.info(
            "[DEV] Company invitation activation link for %s: %s",
            user.email,
            activation_url,
        )
    return _send_transactional_email(
        user,
        "You are invited to CarbonPilot",
        text_content,
        html_content,
    )


def send_account_rejection_email(user):
    first_name = user.first_name or user.get_full_name() or user.email
    role = user.get_role_display()
    html_content = render_to_string(
        "emails/account_rejection.html",
        {"first_name": first_name, "role": role},
    )
    text_content = (
        f"Hello {first_name},\n\n"
        "Thank you for your interest in CarbonPilot.\n\n"
        "Unfortunately, your account request has not been approved at this time.\n\n"
        f"Role requested: {role}\n\n"
        "If appropriate, contact the CarbonPilot administrator for more information.\n\n"
        "Regards,\nCarbonPilot Team"
    )
    return _send_transactional_email(
        user,
        "CarbonPilot account request",
        text_content,
        html_content,
    )


def send_account_approval_email(user, temporary_password):
    first_name = user.first_name or user.get_full_name() or user.email
    login_url = build_login_url()
    context = {
        "first_name": first_name,
        "email": user.email,
        "role": user.get_role_display(),
        "temporary_password": temporary_password,
        "login_url": login_url,
    }
    html_content = render_to_string("emails/account_approval.html", context)
    text_content = (
        f"Hello {first_name},\n\n"
        "Your CarbonPilot account has been approved.\n\n"
        f"Email: {user.email}\n"
        f"Temporary password: {temporary_password}\n"
        f"Role: {user.get_role_display()}\n\n"
        f"Sign in: {login_url}\n\n"
        "After signing in, you must immediately choose a new password. "
        "Do not share this temporary password.\n\nRegards,\nCarbonPilot Team"
    )
    return _send_transactional_email(
        user,
        "Your CarbonPilot account has been approved",
        text_content,
        html_content,
    )