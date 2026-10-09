"""
assessments.services
====================

All rules that decide who may open an exam, how time and penalties work, and
how an attempt is submitted. Views stay thin; every rule is testable here.
"""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .marking import has_open_questions, mark_objective, q2, recompute_final
from .models import (VIOLATION_TYPES, Attempt, DeviceOpen, Exam, PasswordFailure,
                     RosterEntry, Violation, normalise_reg_no)

TAB_STALE_SECONDS = 30          # a tab whose heartbeat is older than this no longer holds the lock
HEARTBEAT_GAP_SECONDS = 60      # a longer silence is logged (log-only) for the trainer
SUBMIT_GRACE_SECONDS = 20       # autosave/submit still accepted this long after the deadline
PASSWORD_WINDOW_MINUTES = 10
PASSWORD_MAX_FAILURES = 10


class Blocked(Exception):
    """An open was refused. `message` is shown to the candidate."""

    def __init__(self, message, status=403, title="You can't open this exam"):
        super().__init__(message)
        self.message, self.status, self.title = message, status, title


# --------------------------------------------------------------------------- #
# Password throttle
# --------------------------------------------------------------------------- #

def password_throttled(exam, ip):
    since = timezone.now() - timedelta(minutes=PASSWORD_WINDOW_MINUTES)
    return PasswordFailure.objects.filter(exam=exam, ip=ip, created_at__gte=since).count() >= PASSWORD_MAX_FAILURES


def record_password_failure(exam, ip):
    PasswordFailure.objects.create(exam=exam, ip=ip)
    PasswordFailure.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()


# --------------------------------------------------------------------------- #
# Submitting
# --------------------------------------------------------------------------- #

def finalize(attempt, reason):
    """Mark objective sections, freeze the attempt. Caller holds the row lock."""
    if attempt.is_submitted:
        return attempt
    exam = attempt.exam
    total, _per_q, per_section = mark_objective(exam, attempt.answers)
    now = timezone.now()
    attempt.objective_score = total
    attempt.section_scores = {k: str(v) for k, v in per_section.items()}
    attempt.status = Attempt.SUBMITTED
    attempt.submit_reason = reason
    attempt.submitted_at = min(now, attempt.end_at) if (reason == "time" and attempt.end_at) else now
    attempt.marking_complete = not has_open_questions(exam)
    recompute_final(attempt)
    attempt.save()
    return attempt


def expire_if_due(attempt):
    """Auto-submit an in-progress attempt whose deadline (+ grace) has passed."""
    if attempt.is_submitted or not attempt.end_at:
        return attempt
    if timezone.now() >= attempt.end_at + timedelta(seconds=SUBMIT_GRACE_SECONDS):
        with transaction.atomic():
            locked = Attempt.objects.select_for_update().select_related("exam").get(pk=attempt.pk)
            finalize(locked, "time")
            return locked
    return attempt


def sweep_expired(exam):
    for a in exam.attempts.filter(status=Attempt.IN_PROGRESS, end_at__lt=timezone.now()):
        expire_if_due(a)


# --------------------------------------------------------------------------- #
# Opening (entry)
# --------------------------------------------------------------------------- #

def _when(dt):
    return timezone.localtime(dt).strftime("%A %d %B %Y at %H:%M")


def _check_exam_available(exam, started=False):
    """
    `started`: this candidate already started the clock. The late-entry cutoff
    stops new starts only, so someone whose computer crashed can still come back
    (until their own time runs out).
    """
    if not exam.is_open:
        raise Blocked("This assessment is not open right now. Ask your trainer when it starts.",
                      title="Assessment not open")
    if exam.exam_date and timezone.localdate() != exam.exam_date:
        raise Blocked(f"This assessment is scheduled for {exam.exam_date:%A %d %B %Y}.",
                      title="Not today")
    now = timezone.now()
    if exam.opens_at and now < exam.opens_at:
        raise Blocked(f"This assessment opens on {_when(exam.opens_at)}. Come back then.",
                      title="Not open yet")
    if exam.closes_at and now > exam.closes_at and not started:
        raise Blocked(f"Entry closed on {_when(exam.closes_at)}. Candidates who have not started can no longer "
                      "begin. Speak to your trainer.", title="Entry has closed")


def open_exam(exam, *, name, reg_no, device_id, user_agent="", ip=None,
              local_opens=0, local_epoch=None):
    """
    Enforce every access rule and, if they all pass, count one open.

    Returns (attempt, tab_token). Raises Blocked otherwise. The password has
    already been verified by the caller. A refused open never consumes one.
    """
    reg = normalise_reg_no(reg_no)
    existing = Attempt.objects.filter(exam=exam, reg_no=reg).first()
    _check_exam_available(exam, started=bool(existing and existing.end_at))

    # Class list: once an exam has one, only listed candidates can enter, and the listed
    # name and number are used (so typos cannot create a second attempt).
    entry = None
    if exam.roster.exists():
        entry = exam.roster.filter(reg_no=reg).first()
        if entry is None:
            raise Blocked("Your candidate number is not on the class list for this assessment. Check the number "
                          "you typed, or ask your trainer to add you.", title="Not on the class list")
        name = entry.name or name
        reg_no = entry.reg_no_display or reg_no
    if not (name or "").strip():
        raise Blocked("Enter your full name.", status=400, title="Name needed")

    with transaction.atomic():
        attempt, _ = Attempt.objects.select_for_update().get_or_create(
            exam=exam, reg_no=reg,
            defaults={"candidate_name": name.strip(), "reg_no_display": reg_no.strip()})
        attempt = Attempt.objects.select_for_update().select_related("exam").get(pk=attempt.pk)

        if not attempt.is_submitted and attempt.end_at and \
                timezone.now() >= attempt.end_at + timedelta(seconds=SUBMIT_GRACE_SECONDS):
            finalize(attempt, "time")
        if attempt.is_submitted:
            raise Blocked("This assessment has already been submitted for this candidate number.",
                          title="Already submitted")

        device = attempt.devices.filter(device_id=device_id).first()
        if device is None and attempt.devices.count() >= max(exam.max_devices, 1):
            raise Blocked("This assessment was started on another computer or browser. "
                          "Ask your trainer to reset your access if you need to continue here.",
                          title="Different device")

        used = device.opens if device else 0
        if local_epoch is not None and local_epoch == attempt.reset_epoch:
            used = max(used, local_opens)          # localStorage backup can only raise the count
        if used >= exam.max_opens:
            raise Blocked(f"You have already opened this assessment {used} time(s) on this device "
                          f"(the limit is {exam.max_opens}). Ask your trainer to reset your access "
                          "if your computer crashed.", title="No opens left")

        now = timezone.now()
        if attempt.active_tab_token and attempt.last_heartbeat and \
                now - attempt.last_heartbeat < timedelta(seconds=TAB_STALE_SECONDS):
            raise Blocked("This assessment is already open in another tab or window. Close it, wait "
                          "about 30 seconds, then enter again. This attempt did not use up an open.",
                          title="Already open elsewhere")

        if device is None:
            device = DeviceOpen(attempt=attempt, device_id=device_id)
        device.opens = used + 1
        device.last_opened_at = now
        device.user_agent = user_agent[:300]
        device.ip = ip
        device.save()

        token = secrets.token_urlsafe(24)
        attempt.active_tab_token = token
        attempt.last_heartbeat = now
        attempt.save(update_fields=["active_tab_token", "last_heartbeat", "updated_at"])
        return attempt, token


def start_clock(attempt):
    """First click on Start: fix the absolute deadline once. Later opens reuse it."""
    if attempt.end_at is None:
        now = timezone.now()
        attempt.started_at = now
        attempt.end_at = now + timedelta(minutes=attempt.exam.duration_minutes)
        attempt.save(update_fields=["started_at", "end_at", "updated_at"])
    return attempt


def release_tab(attempt, token):
    if attempt.active_tab_token == token:
        attempt.active_tab_token = ""
        attempt.last_heartbeat = None
        attempt.save(update_fields=["active_tab_token", "last_heartbeat", "updated_at"])


def heartbeat(attempt):
    """Refresh the tab lock; log (log-only) an unusually long silence."""
    now = timezone.now()
    if attempt.end_at and attempt.last_heartbeat and \
            (now - attempt.last_heartbeat).total_seconds() > HEARTBEAT_GAP_SECONDS:
        gap = int((now - attempt.last_heartbeat).total_seconds())
        Violation.objects.create(attempt=attempt, vtype="heartbeat_gap", counted=False,
                                 detail=f"No signal from the exam page for {gap}s")
    attempt.last_heartbeat = now
    attempt.save(update_fields=["last_heartbeat", "updated_at"])


# --------------------------------------------------------------------------- #
# Violations
# --------------------------------------------------------------------------- #

def record_violation(attempt, vtype, *, episode="", client_at=None, detail=""):
    """
    Apply the configured penalty. Caller holds the row lock on `attempt`.

    * One counted penalty per away episode: further away events carrying the
      same episode number are logged, uncounted, 0 marks.
    * A type whose penalty is 0 is logged only and does not count toward the limit.
    * Reaching max_violations submits the exam.
    Returns True if this call submitted the attempt.
    """
    spec = VIOLATION_TYPES.get(vtype)
    if spec is None or attempt.is_submitted or not attempt.end_at:
        return False
    exam = attempt.exam
    penalty = exam.penalty_for(vtype)
    counted, deducted, note = False, Decimal("0"), (detail or "")[:200]

    if spec.away and episode and \
            attempt.violations.filter(episode=episode, counted=True).exists():
        note = (note + " · " if note else "") + "same away episode, already penalised"
    elif penalty > 0:
        counted, deducted = True, penalty
        attempt.violation_count += 1
        attempt.penalty_total = q2(Decimal(attempt.penalty_total) + penalty)

    Violation.objects.create(attempt=attempt, vtype=vtype, episode=episode, client_at=client_at,
                             counted=counted, marks_deducted=deducted, detail=note)
    recompute_final(attempt)
    attempt.save(update_fields=["violation_count", "penalty_total", "final_score", "updated_at"])

    if counted and exam.max_violations and attempt.violation_count >= exam.max_violations:
        finalize(attempt, "violations")
        return True
    return False


# --------------------------------------------------------------------------- #
# Trainer actions
# --------------------------------------------------------------------------- #

def reset_opens(attempt, extra_minutes=0):
    """Crash recovery: clear open counts and device binding; keep answers and the clock."""
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().get(pk=attempt.pk)
        attempt.devices.all().delete()
        attempt.reset_epoch += 1
        attempt.active_tab_token = ""
        attempt.last_heartbeat = None
        if extra_minutes and attempt.end_at and not attempt.is_submitted:
            attempt.end_at += timedelta(minutes=extra_minutes)
        attempt.save()
    return attempt


def grant_retake(attempt):
    """
    Give a candidate another chance: keep a one-line record of the old result, then return the
    attempt to a fresh, not-yet-started state. Answers, violations, penalties, marks, the clock
    and device/open counts are cleared. A new access key is issued so the old exam tab and the
    old result link stop working. The candidate must enter again with the password.
    """
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().select_related("exam").get(pk=attempt.pk)
        record = {
            "started_at": attempt.started_at.isoformat() if attempt.started_at else "",
            "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else "",
            "status": attempt.status,
            "reason": attempt.get_submit_reason_display() if attempt.submit_reason else "",
            "final_score": str(attempt.final_score) if attempt.final_score is not None else "",
            "out_of": str(attempt.exam.total_marks),
            "violations": attempt.violation_count,
            "penalty": str(attempt.penalty_total),
            "reset_at": timezone.now().isoformat(),
        }
        attempt.previous_attempts = (attempt.previous_attempts or [])[-19:] + [record]
        attempt.devices.all().delete()
        attempt.violations.all().delete()
        attempt.status = Attempt.IN_PROGRESS
        attempt.submit_reason = ""
        attempt.started_at = attempt.end_at = attempt.submitted_at = None
        attempt.answers, attempt.answers_saved_at, attempt.layout = {}, None, {}
        attempt.active_tab_token, attempt.last_heartbeat = "", None
        attempt.reset_epoch += 1
        attempt.access_key = secrets.token_urlsafe(24)
        attempt.violation_count, attempt.penalty_total = 0, Decimal("0")
        attempt.objective_score, attempt.section_scores, attempt.open_marks = Decimal("0"), {}, {}
        attempt.marking_complete, attempt.trainer_comment, attempt.final_score = False, "", None
        attempt.save()
    return attempt


def grant_class_retake(exam, only_submitted=True):
    """
    Give every candidate of an exam another chance. Candidates who never started (they only
    opened the entry page) have nothing to reset and are skipped. Returns the number reset.
    """
    qs = exam.attempts.all()
    if only_submitted:
        qs = qs.filter(status=Attempt.SUBMITTED)
    else:
        qs = qs.filter(Q(status=Attempt.SUBMITTED) | Q(end_at__isnull=False))
    count = 0
    for attempt in qs:
        grant_retake(attempt)
        count += 1
    return count

