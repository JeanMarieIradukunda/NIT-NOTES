"""
assessments.views
=================

Candidate-facing pages: entry (password), the exam page, result and answer sheet.
JSON endpoints used by the exam page live in api.py.
"""

import secrets

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .marking import (SECTION_ORDER, is_answered, option_order, ordered_questions, persist_layout,
                      public_questions, question_max_marks, review_questions)
from .models import (ALL_VIOLATION_LABELS, SECTION_LABELS, Attempt, DeviceOpen, Exam,
                     normalise_reg_no)
from . import services

DEVICE_COOKIE = "nit_dev"
TICKET_SESSION_KEY = "assessment_ticket"
TICKET_TTL_SECONDS = 300
RESULT_SESSION_KEY = "assessment_results"


def client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _blocked(request, title, message, status=403):
    return render(request, "assessments/blocked.html", {"title": title, "message": message}, status=status)


def device_id_for(request):
    """Server-issued random id from the cookie, else the localStorage copy, else a new one."""
    cookie = request.COOKIES.get(DEVICE_COOKIE, "")
    hint = (request.POST.get("device_hint") or "").strip()
    for candidate in (cookie, hint):
        if 16 <= len(candidate) <= 64 and candidate.replace("-", "").replace("_", "").isalnum():
            return candidate
    return secrets.token_urlsafe(24)


def set_device_cookie(response, device_id):
    response.set_cookie(DEVICE_COOKIE, device_id, max_age=60 * 60 * 24 * 365, httponly=True,
                        samesite="Lax", secure=not settings.DEBUG)
    return response


def attempt_has_device(request, attempt, body_device=""):
    ids = [c for c in (request.COOKIES.get(DEVICE_COOKIE, ""), body_device) if c]
    return bool(ids) and attempt.devices.filter(device_id__in=ids).exists()


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #

@never_cache
@require_http_methods(["GET", "POST"])
def entry(request, public_id):
    exam = get_object_or_404(Exam, public_id=public_id.upper())
    ctx = {"exam": exam, "penalties": [p for p in exam.penalty_table() if p[2] > 0],
           "roster_active": exam.roster.exists()}

    if request.method == "GET":
        return render(request, "assessments/entry.html", ctx)

    name = (request.POST.get("name") or "").strip()[:150]
    reg_no = (request.POST.get("reg_no") or "").strip()[:60]
    password = request.POST.get("password") or ""
    ctx.update({"name": name, "reg_no": reg_no})
    ip = client_ip(request) or "unknown"

    if services.password_throttled(exam, ip):
        return _blocked(request, "Too many wrong passwords",
                        "Too many incorrect passwords from this connection. Wait a few minutes and try again.", 429)

    # The password is checked first, so nobody without it learns anything else about the exam.
    if not exam.check_password(password):
        services.record_password_failure(exam, ip)
        ctx["error"] = "That exam password is not correct."
        return render(request, "assessments/entry.html", ctx, status=403)

    if not normalise_reg_no(reg_no) or (not name and not ctx["roster_active"]):
        ctx["error"] = ("Enter your candidate number." if ctx["roster_active"]
                        else "Enter your full name and your candidate number.")
        return render(request, "assessments/entry.html", ctx, status=400)

    device_id = device_id_for(request)
    try:
        attempt, tab_token = services.open_exam(
            exam, name=name, reg_no=reg_no, device_id=device_id,
            user_agent=request.META.get("HTTP_USER_AGENT", ""), ip=client_ip(request),
            local_opens=_int_or_none(request.POST.get("local_opens")) or 0,
            local_epoch=_int_or_none(request.POST.get("local_epoch")))
    except services.Blocked as exc:
        response = _blocked(request, exc.title, exc.message, exc.status)
        return set_device_cookie(response, device_id)

    request.session[TICKET_SESSION_KEY] = {
        "public_id": exam.public_id, "key": attempt.access_key, "tab": tab_token,
        "device": device_id, "exp": timezone.now().timestamp() + TICKET_TTL_SECONDS}
    return set_device_cookie(redirect("assessments:take", public_id=exam.public_id), device_id)


# --------------------------------------------------------------------------- #
# The exam page
# --------------------------------------------------------------------------- #

@never_cache
def take(request, public_id):
    """
    Serves the exam once per ticket. A refresh finds no ticket and sends the
    candidate back to the password screen — which is exactly what makes a
    refresh cost an open.
    """
    exam = get_object_or_404(Exam, public_id=public_id.upper())
    ticket = request.session.pop(TICKET_SESSION_KEY, None)
    if not ticket or ticket.get("public_id") != exam.public_id or ticket.get("exp", 0) < timezone.now().timestamp():
        return redirect("assessments:entry", public_id=exam.public_id)

    attempt = Attempt.objects.select_related("exam").filter(access_key=ticket["key"]).first()
    if attempt is None or attempt.is_submitted or attempt.active_tab_token != ticket["tab"] \
            or not attempt_has_device(request, attempt, ticket.get("device", "")):
        return redirect("assessments:entry", public_id=exam.public_id)

    persist_layout(exam, attempt)
    device = attempt.devices.get(device_id=ticket["device"])
    now = timezone.now()
    config = {
        "exam": {"title": exam.title, "durationMinutes": exam.duration_minutes,
                 "maxViolations": exam.max_violations, "totalMarks": str(exam.total_marks)},
        "candidate": {"name": attempt.candidate_name, "regNo": attempt.reg_no_display or attempt.reg_no},
        "questions": public_questions(exam, attempt),          # no answer key in here
        "answers": attempt.answers or {},
        "endAtMs": int(attempt.end_at.timestamp() * 1000) if attempt.end_at else None,
        "serverNowMs": int(now.timestamp() * 1000),
        "violationCount": attempt.violation_count,
        "penaltyTotal": float(attempt.penalty_total),
        "penalties": [{"key": k, "label": l, "marks": float(p)} for k, l, p, _a in exam.penalty_table() if p > 0],
        "tabToken": ticket["tab"],
        "deviceToken": ticket["device"],
        "opens": device.opens, "maxOpens": exam.max_opens, "epoch": attempt.reset_epoch,
        "storageKey": f"{exam.public_id}:{attempt.reg_no}",
        "api": {a: f"/exam/api/{attempt.access_key}/{a}/"
                for a in ("start", "autosave", "heartbeat", "violation", "submit", "release")},
        "resultUrl": f"/exam/result/{attempt.access_key}/",
        "sections": [{"key": s, "label": SECTION_LABELS[s]} for s in SECTION_ORDER],
    }
    return render(request, "assessments/take.html", {"exam": exam, "attempt": attempt, "config": config})


# --------------------------------------------------------------------------- #
# Result and answer sheet
# --------------------------------------------------------------------------- #

def _result_attempt_or_404(request, access_key):
    attempt = Attempt.objects.select_related("exam").filter(access_key=access_key).first()
    if attempt is None:
        raise Http404
    attempt = services.expire_if_due(attempt)
    allowed = access_key in request.session.get(RESULT_SESSION_KEY, []) or attempt_has_device(request, attempt)
    if not allowed or not attempt.is_submitted:
        raise Http404
    return attempt


def _sheet_rows(attempt):
    exam = attempt.exam
    qs = ordered_questions(exam, attempt)                 # the order this candidate saw
    maxima = question_max_marks(exam, qs)
    rows, n = [], 0
    for q in qs:
        n += 1
        raw = (attempt.answers or {}).get(str(q.id))
        text = ""
        if q.section == "mcq":
            opts = q.payload.get("options", [])
            picked = raw if isinstance(raw, list) else ([raw] if isinstance(raw, int) and not isinstance(raw, bool) else [])
            text = "; ".join(opts[i] for i in option_order(exam, attempt, q) if i in picked and 0 <= i < len(opts))
        elif q.section in ("fill", "open") and isinstance(raw, str):
            text = raw
        elif q.section == "match" and isinstance(raw, dict):
            right = {}
            from .marking import right_id
            for i, r in enumerate(q.payload.get("right", [])):
                right[right_id(q.id, i)] = r
            left = q.payload.get("left", [])
            text = "; ".join(f"{left[int(k)]} → {right.get(v, '?')}" for k, v in sorted(raw.items(), key=lambda kv: int(kv[0])))
        rows.append({"n": n, "section": SECTION_LABELS[q.section], "question": q.text,
                     "answer": text, "answered": bool(text), "max": maxima[q.id]})
    return rows


def _remember_result(request, attempt):
    keys = request.session.get(RESULT_SESSION_KEY, [])
    if attempt.access_key not in keys:
        request.session[RESULT_SESSION_KEY] = keys + [attempt.access_key]


@never_cache
def result(request, access_key):
    attempt = _result_attempt_or_404(request, access_key)
    exam = attempt.exam
    ctx = {
        "attempt": attempt, "exam": exam,
        "sections": [{"label": SECTION_LABELS[s], "max": exam.section_marks(s),
                      "score": attempt.section_scores.get(s) if s != "open" else attempt.open_total}
                     for s in SECTION_ORDER if exam.section_marks(s) > 0],
        "violations": attempt.violations.filter(counted=True),
        "open_pending": exam.questions.filter(section="open").exists() and not attempt.marking_complete,
    }
    return render(request, "assessments/result.html", ctx)


@never_cache
def answer_review(request, access_key):
    """Question-by-question answers, only after submission and only if the trainer has enabled it."""
    attempt = _result_attempt_or_404(request, access_key)
    exam = attempt.exam
    if not exam.show_answers:
        return render(request, "assessments/blocked.html", {
            "title": "Answers not released yet",
            "message": "Your trainer has not released the answers for this assessment. Check again later."}, status=403)
    rows = review_questions(exam, attempt)
    return render(request, "assessments/answer_review.html", {
        "attempt": attempt, "exam": exam, "rows": rows, "show_marks": exam.show_results,
        "counts": {k: sum(1 for r in rows if r["status"] == k) for k in ("correct", "partial", "wrong", "blank")}})


@never_cache
def answer_sheet(request, access_key):
    attempt = _result_attempt_or_404(request, access_key)
    exam = attempt.exam
    html = render_to_string("assessments/answer_sheet.html", {
        "attempt": attempt, "exam": exam, "rows": _sheet_rows(attempt),
        "violations": attempt.violations.all(), "labels": ALL_VIOLATION_LABELS,
        "generated": timezone.now(), "show_score": exam.show_results,
        "open_pending": exam.questions.filter(section="open").exists() and not attempt.marking_complete,
        "brand": {"institution": "Padri Vjeko Centre TSS", "department": "Department of Information Technology"},
    })
    filename = f"answer-sheet-{exam.public_id}-{attempt.reg_no}"
    try:
        from weasyprint import HTML
        pdf = HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()
    except Exception:
        # No PDF engine on this server: show the sheet on screen (to print or save as PDF
        # from the browser). It is never offered as an HTML file download.
        return render(request, "assessments/answer_sheet.html", {
            "attempt": attempt, "exam": exam, "rows": _sheet_rows(attempt),
            "violations": attempt.violations.all(), "generated": timezone.now(), "show_score": exam.show_results,
            "open_pending": exam.questions.filter(section="open").exists() and not attempt.marking_complete,
            "brand": {"institution": "Padri Vjeko Centre TSS", "department": "Department of Information Technology"},
            "on_screen": True})
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return response
