"""
accounts.throttle
=================

Pauses sign-in after repeated wrong passwords.

* 5 failed attempts for one account within 15 minutes pauses that account's sign-in for the
  rest of the window (so guessing a trainer's password stops being worth trying).
* 30 failures from one network address in the same window pauses that address.

Failures are stored in the database (``LoginFailure``) rather than in memory, so the pause holds
across every server instance. If the table does not exist yet (the migration has not been run),
everything here quietly does nothing and sign-in works exactly as before.
"""

import logging
from datetime import timedelta

from django.db import DatabaseError
from django.utils import timezone

from .models import LoginFailure

log = logging.getLogger(__name__)

MAX_FAILURES = 5            # per account
MAX_FAILURES_PER_IP = 30
WINDOW_MINUTES = 15


def clean_username(raw):
    return (raw or "").strip().lower()[:150]


def client_ip(request):
    """The caller's address; behind Vercel's proxy it is the first X-Forwarded-For entry."""
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or request.META.get("REMOTE_ADDR") or None


def _since():
    return timezone.now() - timedelta(minutes=WINDOW_MINUTES)


def is_locked(username, ip):
    try:
        recent = LoginFailure.objects.filter(created_at__gte=_since())
        if recent.filter(username=username).count() >= MAX_FAILURES:
            return True
        return bool(ip) and recent.filter(ip=ip).count() >= MAX_FAILURES_PER_IP
    except DatabaseError:
        log.warning("LoginFailure table unavailable (run `manage.py migrate`); sign-in is not throttled.")
        return False


def record_failure(username, ip):
    try:
        LoginFailure.objects.create(username=username, ip=ip or None)
        # Housekeeping: nothing older than a day is ever needed.
        LoginFailure.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()
    except DatabaseError:
        pass


def clear(username):
    try:
        LoginFailure.objects.filter(username=username).delete()
    except DatabaseError:
        pass
