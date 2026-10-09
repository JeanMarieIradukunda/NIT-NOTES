"""
assessments.staff_views
=======================

Trainer / Administrator workspace: configure exams, write questions, read
results, mark open questions, reset a candidate's opens after a crash.
"""

import csv
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from accounts.roles import is_admin

from . import analysis, evidence, services
from .forms import ExamForm, QuestionForm
from .marking import (OVERRIDABLE_SECTIONS, mark_objective, ordered_questions, q2, question_max_marks,
                      recompute_final)
from .models import (ALL_VIOLATION_LABELS, SECTION_LABELS, SECTION_ORDER, Attempt, Exam, Question)
from .permissions import can_manage_exam, deny, trainer_required
from .views import _sheet_rows, evidence_response


def _exam_or_deny(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if not can_manage_exam(request.user, exam):
        return None, deny(request, "You can only manage your own assessments",
                          "This assessment was created by another Trainer.")
    return exam, None


@trainer_required
def exam_list(request):
    qs = Exam.objects.select_related("module").annotate(n_attempts=Count("attempts", distinct=True))
    if not is_admin(request.user):
        qs = qs.filter(created_by=request.user)
    return render(request, "assessments/exam_list.html", {"exams": qs, "show_owner": is_admin(request.user)})


@trainer_required
def exam_create(request):
    form = ExamForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        exam = form.save(commit=False)
        exam.created_by = request.user
        exam.save()
        if form.generated_password:
            messages.success(request, f"Exam password (shown once, write it down): {form.generated_password}")
        messages.success(request, "Assessment created. Now add its questions.")
        return redirect("assessments:exam_detail", pk=exam.pk)
    return render(request, "assessments/exam_form.html", {"form": form, "exam": None})


@trainer_required
def exam_edit(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    form = ExamForm(request.POST or None, instance=exam, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Assessment settings saved.")
        return redirect("assessments:exam_detail", pk=exam.pk)
    return render(request, "assessments/exam_form.html", {"form": form, "exam": exam})


@trainer_required
def exam_detail(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    questions = list(exam.questions.select_related("key"))
    maxima = question_max_marks(exam, questions)
    groups = []
    for s in SECTION_ORDER:
        items = [q for q in questions if q.section == s]
        for q in items:
            q.max_marks = maxima[q.id]
        groups.append({"key": s, "label": SECTION_LABELS[s], "marks": exam.section_marks(s), "questions": items})
    warnings = []
    for g in groups:
        if g["questions"] and g["marks"] <= 0:
            warnings.append(f"{g['label']} has questions but 0 marks.")
        if not g["questions"] and g["marks"] > 0:
            warnings.append(f"{g['label']} has {g['marks']} marks but no questions.")
    return render(request, "assessments/exam_detail.html", {
        "exam": exam, "groups": groups, "warnings": warnings,
        "entry_url": request.build_absolute_uri(reverse("assessments:entry", args=[exam.public_id])),
        "penalties": exam.penalty_table(), "n_attempts": exam.attempts.count(), "n_roster": exam.roster.count()})


@require_POST
@trainer_required
def exam_toggle_open(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    exam.is_open = not exam.is_open
    exam.save(update_fields=["is_open", "updated_at"])
    messages.success(request, "Assessment is now open to candidates." if exam.is_open else "Assessment closed.")
    return redirect("assessments:exam_detail", pk=exam.pk)


@require_POST
@trainer_required
def exam_toggle_answers(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    exam.show_answers = not exam.show_answers
    exam.save(update_fields=["show_answers", "updated_at"])
    if exam.show_answers:
        waiting = exam.attempts.filter(status=Attempt.IN_PROGRESS).count()
        messages.success(request, "Answers are now visible to candidates who have submitted.")
        if waiting:
            messages.warning(request, f"{waiting} candidate{'s are' if waiting != 1 else ' is'} still in progress. "
                                      "Anyone who finishes early could share the answers, so you may prefer to wait.")
    else:
        messages.success(request, "Answers are hidden from candidates again.")
    return redirect("assessments:exam_detail", pk=exam.pk)


@require_POST
@trainer_required
def exam_delete(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    exam.delete()
    messages.success(request, "Assessment deleted with all its attempts.")
    return redirect("assessments:exam_list")


# --- questions ------------------------------------------------------------ #

@trainer_required
def question_create(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    nxt = (exam.questions.count() or 0) + 1
    form = QuestionForm(request.POST or None, initial={"section": request.GET.get("section", "mcq"), "order": nxt})
    if request.method == "POST" and form.is_valid():
        form.save_to(exam)
        messages.success(request, "Question added.")
        return redirect("assessments:exam_detail", pk=exam.pk)
    return render(request, "assessments/question_form.html", {"form": form, "exam": exam, "question": None})


@trainer_required
def question_edit(request, pk, qid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    q = get_object_or_404(Question.objects.select_related("key"), pk=qid, exam=exam)
    if exam.attempts.exists() and request.method == "GET":
        messages.warning(request, "Candidates have already attempted this assessment. Changing a question now "
                                  "affects how existing answers are marked.")
    form = QuestionForm(request.POST or None, initial=QuestionForm.initial_for(q))
    if request.method == "POST" and form.is_valid():
        form.save_to(exam, q)
        messages.success(request, "Question saved.")
        return redirect("assessments:exam_detail", pk=exam.pk)
    return render(request, "assessments/question_form.html", {"form": form, "exam": exam, "question": q})


@require_POST
@trainer_required
def question_delete(request, pk, qid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    get_object_or_404(Question, pk=qid, exam=exam).delete()
    messages.success(request, "Question deleted.")
    return redirect("assessments:exam_detail", pk=exam.pk)


# --- results -------------------------------------------------------------- #

def _fmt(d):
    return "" if d is None else f"{Decimal(d):.2f}"


@trainer_required
def exam_results(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    services.sweep_expired(exam)
    attempts = list(exam.attempts.annotate(n_opens=Count("devices")).prefetch_related("devices"))
    for a in attempts:
        a.total_opens = sum(d.opens for d in a.devices.all())
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="results-{exam.public_id}.csv"'
        w = csv.writer(response)
        w.writerow(["Candidate", "Candidate no.", "Status", "Submitted by", "Opens", "Violations", "Penalty",
                    "Objective", "Open (marked)", "Final", "Out of", "Open marking"])
        for a in attempts:
            w.writerow([a.candidate_name, a.reg_no_display or a.reg_no, a.get_status_display(),
                        a.get_submit_reason_display(), a.total_opens, a.violation_count, _fmt(a.penalty_total),
                        _fmt(a.objective_score), _fmt(a.open_total), _fmt(a.final_score), _fmt(exam.total_marks),
                        "complete" if a.marking_complete else "pending"])
        return response
    return render(request, "assessments/results.html", {"exam": exam, "attempts": attempts})


def _history(attempt):
    """Earlier chances, newest first, with the stored ISO times turned back into datetimes."""
    out = []
    for h in reversed(attempt.previous_attempts or []):
        h = dict(h)
        h["when"] = parse_datetime(h.get("submitted_at") or h.get("reset_at") or "")
        out.append(h)
    return out


@trainer_required
def attempt_detail(request, pk, aid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt.objects.select_related("exam"), pk=aid, exam=exam)
    attempt = services.expire_if_due(attempt)
    questions = ordered_questions(exam, attempt)           # same order and numbering the candidate saw
    maxima = question_max_marks(exam, questions)
    _total, per_q, _sec = mark_objective(exam, attempt.answers)
    sheet = {r["n"]: r for r in _sheet_rows(attempt)}
    rows = []
    for n, q in enumerate(questions, start=1):
        r = sheet[n]
        override = (attempt.mark_overrides or {}).get(str(q.id))
        rows.append({"q": q, "n": n, "answer": r["answer"], "answered": r["answered"],
                     "max": maxima[q.id], "auto": per_q.get(q.id),
                     "override": override, "overridable": q.section in OVERRIDABLE_SECTIONS,
                     "effective": Decimal(override) if override is not None else per_q.get(q.id),
                     "comment": (attempt.question_comments or {}).get(str(q.id), ""),
                     "given": (attempt.open_marks or {}).get(str(q.id)),
                     "guide": (q.key.data.get("guide") if q.section == "open" and hasattr(q, "key") else "")})
    return render(request, "assessments/attempt_detail.html", {
        "exam": exam, "attempt": attempt, "rows": rows,
        "violations": attempt.violations.all(), "devices": attempt.devices.all(),
        "history": _history(attempt),
        "has_open": any(r["q"].section == "open" for r in rows)})


@require_POST
@trainer_required
def attempt_mark(request, pk, aid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt, pk=aid, exam=exam)
    if not attempt.is_submitted:
        messages.error(request, "Open questions can be marked once the candidate has submitted.")
        return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)
    maxima = question_max_marks(exam)
    marks, bad = {}, []
    for q in exam.questions.filter(section="open"):
        raw = (request.POST.get(f"mark_{q.id}") or "").strip()
        if raw == "":
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            bad.append(q.id)
            continue
        if not value.is_finite() or value < 0 or value > maxima[q.id]:
            bad.append(q.id)
            continue
        marks[str(q.id)] = str(q2(value))
    # Adjusted marks for auto-marked multiple-choice / fill-in questions. A blank box means "use the
    # automatic mark", and a value equal to the automatic mark is not stored as an adjustment.
    _t, auto_q, _s = mark_objective(exam, attempt.answers)
    overrides = {}
    for q in exam.questions.filter(section__in=OVERRIDABLE_SECTIONS):
        raw = (request.POST.get(f"omark_{q.id}") or "").strip()
        if raw == "":
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            bad.append(q.id)
            continue
        if not value.is_finite() or value < 0 or value > maxima[q.id]:
            bad.append(q.id)
            continue
        value = q2(value)
        if value != auto_q.get(q.id):
            overrides[str(q.id)] = str(value)
    comments = {}
    for q in exam.questions.all():
        text = (request.POST.get(f"comment_{q.id}") or "").strip()[:1000]
        if text:
            comments[str(q.id)] = text
    if bad:
        messages.error(request, "Some marks were not saved: each must be a number from 0 up to that question's maximum.")
        return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)
    open_ids = [str(i) for i in exam.questions.filter(section="open").values_list("id", flat=True)]
    total, _per_q, per_section = mark_objective(exam, attempt.answers, overrides)
    attempt.mark_overrides, attempt.question_comments = overrides, comments
    attempt.objective_score = total
    attempt.section_scores = {k: str(v) for k, v in per_section.items()}
    attempt.open_marks = marks
    attempt.trainer_comment = (request.POST.get("trainer_comment") or "")[:2000]
    attempt.marking_complete = all(i in marks for i in open_ids)
    recompute_final(attempt)
    attempt.save()
    messages.success(request, "Marks saved." + ("" if attempt.marking_complete else " Some open questions are still unmarked."))
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@require_POST
@trainer_required
def attempt_reset(request, pk, aid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt, pk=aid, exam=exam)
    try:
        extra = max(0, min(int(request.POST.get("extra_minutes") or 0), 240))
    except ValueError:
        extra = 0
    services.reset_opens(attempt, extra)
    messages.success(request, "Opens reset. The candidate can enter again; their saved answers and the exam "
                              "clock are kept" + (f", and {extra} minutes were added." if extra else "."))
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@require_POST
@trainer_required
def attempt_retake(request, pk, aid):
    """Give the candidate another full chance (fresh paper, clock and opens). The old score is kept as a note."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt, pk=aid, exam=exam)
    services.grant_retake(attempt)
    messages.success(request, f"{attempt.candidate_name} can now take the assessment again from the start. "
                              "Their earlier result is kept in the history below.")
    if not exam.is_open:
        messages.warning(request, "The assessment is closed. Open it so the candidate can enter.")
    elif exam.closes_at and timezone.now() > exam.closes_at:
        messages.warning(request, "The entry cutoff has passed, so the candidate cannot start. "
                                  "Change the cutoff in the assessment settings.")
    if request.POST.get("next") == "results":
        return redirect("assessments:exam_results", pk=exam.pk)
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@require_POST
@trainer_required
def exam_retake_all(request, pk):
    """Give the whole class another chance in one click."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    only_submitted = request.POST.get("scope") != "everyone"
    count = services.grant_class_retake(exam, only_submitted=only_submitted)
    if not count:
        messages.info(request, "No one to reset: "
                      + ("nobody has submitted yet." if only_submitted else "nobody has started yet."))
        return redirect("assessments:exam_results", pk=exam.pk)
    messages.success(request, f"{count} candidate{'s' if count != 1 else ''} can now take the assessment again "
                              "from the start. Their earlier results are kept in each candidate's history.")
    if not exam.is_open:
        messages.warning(request, "The assessment is closed. Open it so the class can enter.")
    elif exam.closes_at and timezone.now() > exam.closes_at:
        messages.warning(request, "The entry cutoff has passed, so candidates cannot start. "
                                  "Change the cutoff in the assessment settings.")
    return redirect("assessments:exam_results", pk=exam.pk)


@require_POST
@trainer_required
def attempt_force_submit(request, pk, aid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    from django.db import transaction
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().select_related("exam").get(pk=aid, exam=exam)
        services.finalize(attempt, "teacher")
    messages.success(request, "Attempt submitted with the answers saved so far.")
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=aid)


@trainer_required
def exam_analysis(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    services.sweep_expired(exam)
    summary, items = analysis.analyse(exam)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="item-analysis-{exam.public_id}.csv"'
        w = csv.writer(response)
        w.writerow(["No.", "Section", "Question", "Max marks", "Answered", "Fully correct", "% fully correct",
                    "Average mark", "Discrimination", "Flags"])
        for it in items:
            w.writerow([it["n"], it["label"], it["text"][:200], it["max"], it["answered"], it["full"],
                        "" if it["pct_correct"] is None else it["pct_correct"],
                        "" if it["avg"] is None else it["avg"], "" if it["disc"] is None else it["disc"],
                        " | ".join(f[1] for f in it["flags"])])
        return response
    return render(request, "assessments/analysis.html", {"exam": exam, "summary": summary, "items": items})


# --------------------------------------------------------------------------- #
# Evidence downloads
# --------------------------------------------------------------------------- #

@trainer_required
def attempt_evidence(request, pk, aid):
    """One candidate's one-page evidence record (always with marks)."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt.objects.select_related("exam"), pk=aid, exam=exam)
    attempt = services.expire_if_due(attempt)
    if not attempt.is_submitted:
        messages.error(request, "An evidence record can be downloaded once the candidate has submitted.")
        return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)
    return evidence_response(request, attempt, trainer=True)


@trainer_required
def exam_evidence(request, pk):
    """Every submitted candidate in one PDF: a register page, then each marks sheet on its own page."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    services.sweep_expired(exam)
    everyone = list(exam.attempts.select_related("exam").prefetch_related("devices", "violations"))
    submitted = [a for a in everyone if a.is_submitted]
    if not submitted:
        messages.error(request, "No candidate has submitted yet, so there is no evidence to download.")
        return redirect("assessments:exam_results", pk=exam.pk)
    skipped = len(everyone) - len(submitted)
    try:
        pdf = evidence.bundle_pdf(exam, submitted, skipped)
    except Exception:
        return render(request, "assessments/evidence_bundle.html", evidence.bundle_html_context(exam, submitted, skipped))
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="evidence-{exam.public_id}-all-candidates.pdf"'
    return response
