"""
assessments.dashboard
=====================

Open assessments for the student dashboard's highlighted "New assessment" links.

An assessment is listed while it is open, its trainer has left "Show on the student
dashboard" ticked, its exam date has not passed and its late-entry cutoff has not
passed. Nothing secret is shown: the password is still needed to enter.
"""

import logging
from datetime import timedelta

from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone

from .models import Exam

logger = logging.getLogger(__name__)

NEW_FOR_DAYS = 7      # an assessment created within this many days carries the "New" badge
MAX_SHOWN = 3


def _when(exam, now, today):
    local = timezone.localtime
    parts = []
    if exam.opens_at and exam.opens_at > now:
        parts.append("Opens " + local(exam.opens_at).strftime("%a %d %b, %H:%M"))
    elif exam.exam_date == today:
        parts.append("Today")
    elif exam.exam_date:
        parts.append(exam.exam_date.strftime("%a %d %b"))
    if exam.closes_at:
        closes = local(exam.closes_at)
        parts.append("start by " + closes.strftime("%H:%M" if closes.date() == today else "%a %d %b, %H:%M"))
    return " · ".join(parts)


def dashboard_assessments(limit=MAX_SHOWN, now=None):
    """Newest first. Each exam gets `is_new` and `when_label` attributes for the template."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    try:
        exams = list(
            Exam.objects.filter(is_open=True, show_on_dashboard=True)
            .filter(Q(exam_date__isnull=True) | Q(exam_date__gte=today))
            .filter(Q(closes_at__isnull=True) | Q(closes_at__gte=now))
            .select_related("module").order_by("-created_at")[:limit])
    except DatabaseError:
        # Most likely the latest migration has not been applied yet. The landing page must
        # keep working for students, so show no assessments instead of an error page.
        logger.warning("Dashboard assessments unavailable; has `manage.py migrate` been run?", exc_info=True)
        return []
    for exam in exams:
        exam.is_new = exam.created_at >= now - timedelta(days=NEW_FOR_DAYS)
        exam.when_label = _when(exam, now, today)
    return exams
