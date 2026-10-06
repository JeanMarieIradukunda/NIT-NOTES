"""
assessments.api
===============

JSON endpoints the exam page calls. Every one requires:
  * the attempt's access key (in the URL),
  * the per-page-load tab token (only the page that was served holds it), and
  * a device that was recorded for this attempt (cookie, or the localStorage copy).

The tab token is an unguessable bearer secret, so these endpoints are exempt
from Django's CSRF token (navigator.sendBeacon cannot send custom headers).
A page that is no longer the active tab gets `superseded: true` and locks itself.
"""

import json
from datetime import timedelta
from functools import wraps

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .marking import clean_answer
from .models import Attempt, Question
from .views import attempt_has_device
from . import services

MAX_BODY_BYTES = 400_000


def _state(attempt):
    now = timezone.now()
    return {
        "ok": True,
        "submitted": attempt.is_submitted,
        "submitReason": attempt.submit_reason,
        "violationCount": attempt.violation_count,
        "penaltyTotal": float(attempt.penalty_total),
        "endAtMs": int(attempt.end_at.timestamp() * 1000) if attempt.end_at else None,
        "serverNowMs": int(now.timestamp() * 1000),
    }


def exam_api(view):
    @csrf_exempt
    @require_POST
    @wraps(view)
    def wrapper(request, access_key):
        if len(request.body) > MAX_BODY_BYTES:
            return JsonResponse({"ok": False, "error": "too_large"}, status=413)
        try:
            body = json.loads(request.body or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

        with transaction.atomic():
            attempt = (Attempt.objects.select_for_update().select_related("exam")
                       .filter(access_key=access_key).first())
            if attempt is None:
                return JsonResponse({"ok": False, "error": "not_found"}, status=404)
            if not attempt_has_device(request, attempt, str(body.get("device", ""))):
                return JsonResponse({"ok": False, "error": "device"}, status=403)
            if not attempt.is_submitted and attempt.active_tab_token != str(body.get("tab", "")):
                return JsonResponse({"ok": False, "superseded": True}, status=409)
            if attempt.is_submitted and view.__name__ not in ("heartbeat", "submit", "release"):
                return JsonResponse({**_state(attempt), "ok": False, "error": "submitted"}, status=409)
            return view(request, attempt, body)
    return wrapper


@exam_api
def start(request, attempt, body):
    services.start_clock(attempt)
    return JsonResponse(_state(attempt))


@exam_api
def heartbeat(request, attempt, body):
    if not attempt.is_submitted:
        if attempt.end_at and timezone.now() >= attempt.end_at + timedelta(seconds=services.SUBMIT_GRACE_SECONDS):
            services.finalize(attempt, "time")
        else:
            services.heartbeat(attempt)
    return JsonResponse(_state(attempt))


def _merge_answers(attempt, incoming):
    if not isinstance(incoming, dict):
        return
    questions = {str(q.id): q for q in Question.objects.filter(exam=attempt.exam)}
    answers = dict(attempt.answers or {})
    for qid, value in incoming.items():
        q = questions.get(str(qid))
        if q is None:
            continue
        cleaned = clean_answer(q, value)
        if cleaned is None:
            answers.pop(str(qid), None)
        else:
            answers[str(qid)] = cleaned
    attempt.answers = answers
    attempt.answers_saved_at = timezone.now()


@exam_api
def autosave(request, attempt, body):
    if not attempt.end_at:
        return JsonResponse({"ok": False, "error": "not_started"}, status=409)
    if timezone.now() >= attempt.end_at + timedelta(seconds=services.SUBMIT_GRACE_SECONDS):
        services.finalize(attempt, "time")
        return JsonResponse({**_state(attempt), "ok": False, "error": "expired"}, status=409)
    _merge_answers(attempt, body.get("answers"))
    attempt.save(update_fields=["answers", "answers_saved_at", "updated_at"])
    return JsonResponse({**_state(attempt), "savedAt": attempt.answers_saved_at.isoformat()})


@exam_api
def violation(request, attempt, body):
    if not attempt.end_at:
        return JsonResponse({"ok": False, "error": "not_started"}, status=409)
    if attempt.is_expired and timezone.now() >= attempt.end_at + timedelta(seconds=services.SUBMIT_GRACE_SECONDS):
        services.finalize(attempt, "time")
        return JsonResponse(_state(attempt))
    n = body.get("episode")
    # Scoped to this page load so a reopen can never reuse an earlier episode id.
    episode = f"{attempt.active_tab_token[:10]}-{n}" if isinstance(n, int) and 0 <= n < 10**6 else ""
    client_at = parse_datetime(str(body.get("clientAt", ""))) if body.get("clientAt") else None
    auto = services.record_violation(
        attempt, str(body.get("type", "")), episode=episode, client_at=client_at,
        detail=str(body.get("detail", ""))[:120])
    return JsonResponse({**_state(attempt), "autoSubmitted": auto})


@exam_api
def submit(request, attempt, body):
    if not attempt.is_submitted:
        if attempt.end_at and timezone.now() < attempt.end_at + timedelta(seconds=services.SUBMIT_GRACE_SECONDS):
            _merge_answers(attempt, body.get("answers"))
        reason = "time" if attempt.is_expired else "manual"
        services.finalize(attempt, reason)
    return JsonResponse(_state(attempt))


@exam_api
def release(request, attempt, body):
    services.release_tab(attempt, str(body.get("tab", "")))
    return JsonResponse({"ok": True})
