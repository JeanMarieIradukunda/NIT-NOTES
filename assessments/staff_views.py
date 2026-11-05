"""
assessments.staff_views
=======================

Trainer / Administrator workspace: configure exams, write questions, read
results, mark open questions, reset a candidate's opens after a crash.
"""

import csv
import logging
import time
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from accounts.roles import is_admin

from . import analysis, automark, evidence, services
from .forms import ExamForm, QuestionForm
from .marking import (OVERRIDABLE_SECTIONS, correct_set, has_open_questions, is_multi, mark_objective, ordered_questions, q2,
                      question_max_marks, recompute_final)
from .models import (ALL_VIOLATION_LABELS, SECTION_LABELS, SECTION_ORDER, AnswerKey, Attempt, Exam,
                     Question)
from .permissions import can_manage_exam, deny, trainer_required
from .views import _sheet_rows, evidence_response

logger = logging.getLogger(__name__)


def _exam_or_deny(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if not can_manage_exam(request.user, exam):
        return None, deny(request, "You can only manage your own assessments",
                          "This assessment was created by another Trainer.")
    return exam, None


@trainer_required
def exam_list(request):
    qs = Exam.objects.select_related("module").annotate(
        n_attempts=Count("attempts", distinct=True),
        n_submitted=Count("attempts", filter=Q(attempts__status=Attempt.SUBMITTED), distinct=True),
        n_to_mark=Count("attempts", filter=Q(attempts__status=Attempt.SUBMITTED, attempts__marking_complete=False),
                        distinct=True))
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



# --- how far along each candidate is ---------------------------------------- #

STATE_LABELS = {"not_started": "Not started", "in_progress": "In progress", "to_mark": "To mark",
                "auto": "Auto-marked · check", "done": "Marked"}


def attempt_state(a):
    """One word for where a candidate stands: not_started, in_progress, to_mark, auto or done."""
    if not a.is_submitted:
        return "in_progress" if a.end_at else "not_started"
    if not a.marking_complete:
        return "to_mark"
    return "auto" if a.auto_marked_count else "done"


def _open_source(a):
    """auto / trainer / mixed / '' for who gave this attempt's open marks."""
    marks = a.open_marks or {}
    if not marks:
        return ""
    auto = a.auto_marked_count
    return "auto" if auto == len(marks) else "mixed" if auto else "trainer"


def exam_stats(exam, attempts):
    """Counts and score figures for the assessment and results pages."""
    submitted = [a for a in attempts if a.is_submitted]
    scores = [Decimal(a.final_score) for a in submitted if a.final_score is not None]
    total = exam.total_marks
    avg = (sum(scores, Decimal("0")) / len(scores)) if scores else None
    states = [attempt_state(a) for a in attempts]
    return {
        "entered": len(attempts), "submitted": len(submitted),
        "in_progress": states.count("in_progress"),
        "to_mark": states.count("to_mark"), "to_check": states.count("auto"),
        "average": q2(avg) if avg is not None else None,
        "average_pct": q2(avg * 100 / total) if (avg is not None and total) else None,
        "highest": max(scores) if scores else None, "lowest": min(scores) if scores else None,
        "total": total,
    }


def _review_queue(exam):
    """Submitted candidates who still need a trainer: unmarked written answers, or automatic marks not yet checked."""
    return [a for a in exam.attempts.filter(status=Attempt.SUBMITTED) if attempt_state(a) in ("to_mark", "auto")]


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
            q.guide = _guide_row(q, maxima[q.id])
        groups.append({"key": s, "label": GUIDE_LABELS[s], "marks": exam.section_marks(s), "questions": items})
    warnings = []
    for g in groups:
        if g["questions"] and g["marks"] <= 0:
            warnings.append(f"{g['label']} has questions but 0 marks.")
        if not g["questions"] and g["marks"] > 0:
            warnings.append(f"{g['label']} has {g['marks']} marks but no questions.")
    open_rows = [q for q in questions if q.section == "open"]
    no_guide = [q for q in open_rows if not ((q.key.data or {}).get("guide") if hasattr(q, "key") else "")]
    if no_guide and (exam.auto_mark_on_submit or exam.attempts.exists()):
        warnings.append(f"{len(no_guide)} open question{'s have' if len(no_guide) != 1 else ' has'} no marking guide, so "
                        "automatic marking leaves " + ("them" if len(no_guide) != 1 else "it") + " for you.")
    if exam.open_pool_overspent:
        warnings.append("Open questions with their own marks add up to more than the open section's "
                        f"{exam.marks_open:g} marks, so the open questions without their own marks get 0. "
                        "Raise the section marks or give those questions their own marks.")
    return render(request, "assessments/exam_detail.html", {
        "exam": exam, "groups": groups, "warnings": warnings,
        "entry_url": request.build_absolute_uri(reverse("assessments:entry", args=[exam.public_id])),
        "penalties": exam.penalty_table(), "n_attempts": exam.attempts.count(), "n_roster": exam.roster.count(),
        "stats": exam_stats(exam, list(exam.attempts.all())), "n_questions": len(questions),
        "n_open": len(open_rows), "n_open_no_guide": len(no_guide)})


def _pair_index(value, size):
    try:
        i = int(value)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < size else None


GUIDE_LABELS = {**SECTION_LABELS, "mcq": "Multiple choice & True/False"}


def _is_true_false(options):
    return sorted(str(o).strip().lower() for o in options) == ["false", "true"]


def _guide_row(q, marks):
    """What the trainer needs to mark one question: the right answer, or the written-answer guide."""
    try:
        data = q.key.data or {}
    except AnswerKey.DoesNotExist:
        data = {}
    row = {"q": q, "marks": marks}
    if q.section == "mcq":
        options = q.payload.get("options") or []
        right = correct_set(data)           # reads both the old single number and the newer list
        tf = _is_true_false(options)
        row["tf"] = tf
        row["multi"] = is_multi(q)
        row["accept_any"] = (not row["multi"]) and len(right) > 1
        row["options"] = [{"letter": "" if tf else (chr(65 + i) if i < 26 else str(i + 1)),
                           "text": o, "correct": i in right} for i, o in enumerate(options)]
        picked = [o for o in row["options"] if o["correct"]]
        # Short label for the one-line key: "B", "B+D", "B/D" (any one), or "T"/"F" for True/False.
        glue = "+" if row["multi"] else "/"
        row["label"] = (picked[0]["text"][:1].upper() if tf and picked
                        else glue.join(o["letter"] for o in picked))
    elif q.section == "fill":
        row["accepted"] = data.get("accepted") or []
        row["case_sensitive"] = bool(data.get("case_sensitive"))
    elif q.section == "match":
        left, right = q.payload.get("left") or [], q.payload.get("right") or []
        pairs = data.get("pairs") or {}
        row["pairs"] = []
        for i, text in enumerate(left):
            j = _pair_index(pairs.get(str(i)), len(right))
            row["pairs"].append((text, right[j] if j is not None else "—"))
    else:
        row["guide"] = (data.get("guide") or "").strip()
    return row


@trainer_required
def exam_guide(request, pk):
    """Every question with its answer key / marking guide, for the trainer who owns the exam."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    questions = list(exam.questions.select_related("key"))
    maxima = question_max_marks(exam, questions)
    sections, missing, quick_key = [], 0, []
    for s in SECTION_ORDER:
        rows = [_guide_row(q, maxima[q.id]) for q in questions if q.section == s]
        if not rows:
            continue
        if s == "open":
            missing = sum(1 for r in rows if not r["guide"])
        if s == "mcq":      # "1 B · 2 T · 3 B+D": the whole choice / true-false key on one line
            quick_key = [(r["q"].order, r["label"]) for r in rows if r["label"]]
        sections.append({"key": s, "label": GUIDE_LABELS[s], "rows": rows})
    return render(request, "assessments/exam_guide.html", {
        "exam": exam, "sections": sections, "missing": missing, "quick_key": quick_key,
        "n_questions": len(questions)})


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
                    "Objective", "Open (marked)", "Final", "Out of", "Open marking", "Open marks by"])
        for a in attempts:
            w.writerow([a.candidate_name, a.reg_no_display or a.reg_no, a.get_status_display(),
                        a.get_submit_reason_display(), a.total_opens, a.violation_count, _fmt(a.penalty_total),
                        _fmt(a.objective_score), _fmt(a.open_total), _fmt(a.final_score), _fmt(exam.total_marks),
                        "complete" if a.marking_complete else "pending", _open_source(a)])
        return response
    for a in attempts:
        a.state = attempt_state(a)
        a.state_label = STATE_LABELS[a.state]
        a.open_by = _open_source(a)
        a.percent = (q2(Decimal(a.final_score) * 100 / exam.total_marks)
                     if (a.is_submitted and a.final_score is not None and exam.total_marks) else None)
    return render(request, "assessments/results.html", {
        "exam": exam, "attempts": attempts, "stats": exam_stats(exam, attempts),
        "has_open": has_open_questions(exam), "ai_available": automark.ai_available(),
        "ai_counts": automark.count_eligible(exam) if has_open_questions(exam) else None,
        "ai_counts_all": automark.count_eligible(exam, include_ai=True) if has_open_questions(exam) else None,
        "next_to_mark": next((a for a in attempts if a.state in ("to_mark", "auto")), None),
        "counts": {k: sum(1 for a in attempts if a.state == k) for k in STATE_LABELS}})


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
    meta = attempt.open_marking or {}
    rows = []
    for n, q in enumerate(questions, start=1):
        r = sheet[n]
        override = (attempt.mark_overrides or {}).get(str(q.id))
        comments_text = (attempt.question_comments or {}).get(str(q.id), "")
        given = (attempt.open_marks or {}).get(str(q.id))
        m = meta.get(str(q.id)) or {}
        guide = (q.key.data.get("guide") if q.section == "open" and hasattr(q, "key") else "")
        source = ("" if given is None else "auto" if m.get("by") == "auto" else "trainer")
        rows.append({"q": q, "n": n, "answer": r["answer"], "answered": r["answered"],
                     "max": maxima[q.id], "auto": per_q.get(q.id),
                     "override": override, "overridable": q.section in OVERRIDABLE_SECTIONS,
                     "effective": Decimal(override) if override is not None else per_q.get(q.id),
                     "comment": comments_text,
                     "comment_auto": bool(comments_text) and comments_text == (meta.get(f"fb:{q.id}") or ""),
                     "given": given, "guide": guide, "source": source,
                     "method": m.get("method", ""), "note": m.get("note", "") if source == "auto" else ""})
    written = [r for r in rows if r["q"].section == "open"]
    others = [r for r in rows if r["q"].section != "open"]
    siblings = list(exam.attempts.filter(status=Attempt.SUBMITTED))
    ids = [x.pk for x in siblings]
    pos = ids.index(attempt.pk) if attempt.pk in ids else None
    todo = [x for x in siblings if x.pk != attempt.pk and attempt_state(x) in ("to_mark", "auto")]
    return render(request, "assessments/attempt_detail.html", {
        "exam": exam, "attempt": attempt, "rows": rows, "written": written, "others": others,
        "violations": attempt.violations.all(), "devices": attempt.devices.all(),
        "history": _history(attempt), "has_open": bool(written),
        "state": attempt_state(attempt), "state_label": STATE_LABELS[attempt_state(attempt)],
        "ai_available": automark.ai_available(),
        "n_marked": sum(1 for r in written if r["given"] is not None),
        "n_auto": sum(1 for r in written if r["source"] == "auto"),
        "prev_attempt": siblings[pos - 1] if pos else None,
        "next_attempt": siblings[pos + 1] if pos is not None and pos + 1 < len(siblings) else None,
        "position": (pos + 1) if pos is not None else None, "n_submitted": len(siblings),
        "next_to_mark": todo[0] if todo else None, "n_todo": len(todo),
        "total_marks": exam.total_marks,
        "time_allowed": exam.duration_minutes + attempt.extra_minutes,
        "resume_problem": services.resumable_problem(attempt) if attempt.is_submitted else "not submitted",
        "waivable": attempt.violations.filter(counted=True, marks_deducted__gt=0, waived=False).exists(),
        "restorable": attempt.violations.filter(waived=True).exists(),
        "recovery_log": list(reversed(attempt.recovery_log or []))[:8]})


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
    # An automatic mark stays "automatic" until a trainer changes it, or ticks "I have checked them".
    checked = request.POST.get("reviewed_all") == "1"
    old_marks, old_meta, meta = attempt.open_marks or {}, attempt.open_marking or {}, {}
    for qid, value in marks.items():
        m = old_meta.get(qid) or {}
        if m.get("by") == "auto" and not checked and qid in old_marks and q2(Decimal(old_marks[qid])) == Decimal(value):
            meta[qid] = m
    meta.update({k: v for k, v in old_meta.items() if k.startswith("fb:")})     # which comments were written automatically
    attempt.open_marking = meta
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
    if request.POST.get("then") == "next":
        todo = [x for x in _review_queue(exam) if x.pk != attempt.pk]
        if todo:
            return redirect("assessments:attempt_detail", pk=exam.pk, aid=todo[0].pk)
        messages.info(request, "That was the last candidate waiting for marking.")
        return redirect("assessments:exam_results", pk=exam.pk)
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


def _automark_message(request, res, *, scope):
    if res["marked"]:
        bits = [f"Auto-marked {res['marked']} written answer{'s' if res['marked'] != 1 else ''}{scope}."]
        if res["kept"]:
            bits.append(f"{res['kept']} marked by a trainer {'was' if res['kept'] == 1 else 'were'} left alone.")
        messages.success(request, " ".join(bits) + " Check them, and change any mark you disagree with.")
    elif res["kept"] and not res["no_guide"]:
        messages.info(request, "Nothing to mark: every written answer already has a trainer's mark. "
                               "Tick “also replace my marks” to mark them again.")
    else:
        messages.info(request, "No written answers were marked.")
    if res["no_guide"]:
        messages.warning(request, f"{res['no_guide']} answer{'s were' if res['no_guide'] != 1 else ' was'} skipped "
                                  "because the question has no marking guide. Add guides under Marking guide.")
    if res["ai_fallback"]:
        messages.warning(request, f"The AI service was not available for {res['ai_fallback']} answer"
                                  f"{'s' if res['ai_fallback'] != 1 else ''}, so key points were compared instead.")
    if res["ai_limit"]:
        messages.info(request, f"AI marking stops after {automark.AI_CALLS_PER_REQUEST} answers at a time. "
                               f"{res['ai_limit']} left: run auto-mark again to continue.")


def _method(request):
    return automark.METHOD_AI if (request.POST.get("method") == "ai" and automark.ai_available()) else automark.METHOD_KEYWORDS


@require_POST
@trainer_required
def attempt_automark(request, pk, aid):
    """Auto-mark this candidate's written answers. A trainer's own marks are kept unless asked otherwise."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt.objects.select_related("exam"), pk=aid, exam=exam)
    if not attempt.is_submitted:
        messages.error(request, "Written answers can be marked once the candidate has submitted.")
        return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)
    method = _method(request)
    budget = [automark.AI_CALLS_PER_REQUEST] if method == automark.METHOD_AI else None
    # AI re-marking never replaces a trainer's own marks, whatever the box says.
    overwrite = request.POST.get("overwrite") == "1" and method != automark.METHOD_AI
    deadline = time.monotonic() + automark.AI_TIME_BUDGET if method == automark.METHOD_AI else None
    res = automark.automark_attempt(attempt, method=method, overwrite=overwrite, ai_budget=budget, deadline=deadline)
    _automark_message(request, res, scope="")
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@require_POST
@trainer_required
def exam_remark_ai(request, pk):
    """
    Re-mark, with AI, the written answers no trainer has marked. One batch per request; the page repeats the
    call (passing ``since`` back) until nothing is left. A trainer's own marks are never changed.
    """
    exam, denied = _exam_or_deny(request, pk)
    wants_json = "application/json" in request.headers.get("Accept", "")
    if denied:
        return JsonResponse({"error": "You do not have access to this assessment."}, status=403) if wants_json else denied
    if not automark.ai_available():
        msg = "AI marking is not set up. Add GROQ_API_KEY to the server settings."
        if wants_json:
            return JsonResponse({"error": msg}, status=400)
        messages.error(request, msg)
        return redirect("assessments:exam_results", pk=exam.pk)
    services.sweep_expired(exam)
    include_ai = request.POST.get("include_ai") == "1"
    res = automark.remark_with_ai(exam, include_ai=include_ai, since=request.POST.get("since") or None)
    if wants_json:
        return JsonResponse(res)
    _automark_message(request, {**res, "kept": 0, "ai_limit": 0}, scope="")
    if res["remaining"]:
        messages.info(request, f"{res['remaining']} answer{'s' if res['remaining'] != 1 else ''} still to re-mark. Run it again to continue.")
    return redirect("assessments:exam_results", pk=exam.pk)


@require_POST
@trainer_required
def exam_automark(request, pk):
    """Auto-mark the written answers of every submitted candidate."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    services.sweep_expired(exam)
    res = automark.automark_exam(exam, method=_method(request), overwrite=request.POST.get("overwrite") == "1")
    _automark_message(request, res, scope=f" for {res['attempts']} candidate{'s' if res['attempts'] != 1 else ''}")
    return redirect("assessments:exam_results", pk=exam.pk)


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
    except Exception as exc:
        logger.warning("Evidence pack PDF for exam %s could not be made; showing the print page instead.",
                       exam.pk, exc_info=True)
        ctx = evidence.bundle_html_context(exam, submitted, skipped)
        return render(request, "assessments/evidence_bundle.html",
                      {**ctx, "pdf_problem": evidence.describe_pdf_error(exc)})
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="evidence-{exam.public_id}-all-candidates.pdf"'
    return response


# --------------------------------------------------------------------------- #
# Time, deductions and recovery after a power or network cut
# --------------------------------------------------------------------------- #

def _minutes(request, default=0):
    try:
        return int(request.POST.get("minutes") or default)
    except ValueError:
        return -1


def _who(request):
    return request.user.get_username()


@require_POST
@trainer_required
def attempt_extend(request, pk, aid):
    """Give one candidate more time (before or during the assessment)."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt, pk=aid, exam=exam)
    try:
        services.extend_attempt(attempt, _minutes(request), by=_who(request))
        messages.success(request, f"Added {_minutes(request)} minutes for {attempt.candidate_name}. "
                                  "If they are working now, their clock updates within a few seconds.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@require_POST
@trainer_required
def attempt_resume(request, pk, aid):
    """Reopen an attempt that was submitted automatically, for example during a power cut."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt, pk=aid, exam=exam)
    try:
        services.resume_attempt(attempt, _minutes(request, 15), waive=request.POST.get("waive") == "1", by=_who(request))
        messages.success(request, f"{attempt.candidate_name} can enter again with {_minutes(request, 15)} minutes. "
                                  "Their saved answers are kept.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@require_POST
@trainer_required
def attempt_waive(request, pk, aid):
    """Remove or restore the marks deducted for violations: all of them, or one."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    attempt = get_object_or_404(Attempt, pk=aid, exam=exam)
    restore = request.POST.get("action") == "restore"
    vid = request.POST.get("violation")
    vids = [int(vid)] if (vid or "").isdigit() else None
    changed = services.waive_violations(attempt, vids=vids, waive=not restore, by=_who(request))
    if changed:
        messages.success(request, ("Deductions restored" if restore else "Deductions removed") +
                                  f" for {changed} violation{'s' if changed != 1 else ''}. The score has been updated.")
    else:
        messages.info(request, "Nothing to change.")
    return redirect("assessments:attempt_detail", pk=exam.pk, aid=attempt.pk)


@trainer_required
def exam_time(request, pk):
    """One page for time and outage recovery: extend the assessment, or recover after a power or network cut."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    try:
        hours = min(max(int(request.GET.get("hours") or 3), 1), 48)
    except ValueError:
        hours = 3
    preview = services.recovery_preview(exam, hours)
    return render(request, "assessments/exam_time.html", {
        "exam": exam, "hours": hours, "preview": preview, "hours_options": (1, 3, 6, 12, 24, 48),
        "max_minutes": services.MAX_EXTENSION_MINUTES,
        "running": exam.attempts.filter(status=Attempt.IN_PROGRESS, end_at__isnull=False).count()})


@require_POST
@trainer_required
def exam_extend(request, pk):
    """Make the assessment longer for everyone, including candidates working right now."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    try:
        running = services.extend_exam(exam, _minutes(request), include_running=request.POST.get("include_running") == "1",
                                       by=_who(request))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("assessments:exam_time", pk=exam.pk)
    msg = f"The assessment is now {exam.duration_minutes} minutes long."
    if request.POST.get("include_running") == "1":
        msg += f" {running} candidate{'s' if running != 1 else ''} working now got the extra time automatically."
    messages.success(request, msg)
    return redirect("assessments:exam_time", pk=exam.pk)


@require_POST
@trainer_required
def exam_recover(request, pk):
    """After a power or network cut: get candidates back in, give time back, resume auto-submitted attempts."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    minutes = _minutes(request, 15)
    if not 1 <= minutes <= services.MAX_EXTENSION_MINUTES:
        messages.error(request, f"Add between 1 and {services.MAX_EXTENSION_MINUTES} minutes.")
        return redirect("assessments:exam_time", pk=exam.pk)
    try:
        hours = min(max(int(request.POST.get("hours") or 0), 0), 48)
    except ValueError:
        hours = 0
    res = services.recover_exam(
        exam, minutes=minutes, extend_running=request.POST.get("extend_running") == "1",
        reset_running=request.POST.get("reset_running") == "1",
        resume_hours=hours if request.POST.get("resume") == "1" else 0,
        waive=request.POST.get("waive") == "1", by=_who(request))
    bits = []
    if res["reset"]:
        bits.append(f"access reset for {res['reset']}")
    if res["extended"]:
        bits.append(f"{minutes} minutes added for {res['extended']}")
    if res["resumed"]:
        bits.append(f"{res['resumed']} attempt{'s' if res['resumed'] != 1 else ''} resumed")
    if res["waived"]:
        bits.append(f"deductions removed for {res['waived']}")
    messages.success(request, "Recovery done: " + (", ".join(bits) if bits else "nothing needed changing") + ".")
    if res["skipped"]:
        messages.warning(request, "Not resumed (already marked by a trainer): " + ", ".join(res["skipped"][:8]))
    return redirect("assessments:exam_time", pk=exam.pk)
