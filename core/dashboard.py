"""
core.dashboard
==============

One URL (``core:dashboard``), three focused landing pages:

* **Student**       anyone who is not staff (including anonymous visitors):
                    open assessments, their levels, the newest published work.
* **Trainer**       their own notes, activities and assessments at a glance.
* **Administrator** platform-wide totals plus a short "needs attention" list.

Each builder returns a *small* context on purpose. Every number or list on a
dashboard has to earn its place; anything else belongs on the page it links to.
"""

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.db.models import Count, Q
from django.urls import reverse

from accounts.roles import TRAINER, is_admin, is_trainer
from .models import Activity, Module, ModuleNote, StudentActivity, Trade

RECENT = 5          # rows in any "latest" / "recent" list
SAVED = 3           # saved lessons shown to a signed-in student


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _stat(url_name, icon, label, value, sub, warn=False):
    """One headline number. ``warn`` tints the sub-line amber (only when it is non-zero)."""
    return {"url": reverse(url_name), "icon": icon, "label": label,
            "value": value, "sub": sub, "warn": bool(warn)}


def _who(user):
    return (user.first_name or user.username) if user else ""


def _item(kind, obj, url, when, status=None, with_who=True):
    """One row in a feed. ``status`` ("Published"/"Draft") is only set for staff lists."""
    return {
        "kind": kind,                       # "Notes" | "Activity"
        "title": obj.title,
        "code": obj.module.code,
        "module": obj.module.name,
        "when": when,
        "url": url,
        "is_pdf": obj.is_pdf,
        "who": _who(obj.uploaded_by) if with_who else "",
        "status": status,
    }


def _merged(notes, activities, *, staff, with_who=True):
    """Interleave notes and activities newest-first and keep the top few.

    ``notes`` / ``activities`` are querysets already filtered and ordered; each is
    cut to RECENT before merging, so the work done here is tiny.
    """
    rows = []
    for n in notes.select_related("module", "uploaded_by")[:RECENT]:
        url = reverse("core:note_edit", args=[n.pk]) if staff else n.get_absolute_url()
        when = (n.updated_at if staff else (n.published_at or n.updated_at))
        rows.append(_item("Notes", n, url, when,
                          ("Published" if n.is_published else "Draft") if staff else None, with_who))
    for a in activities.select_related("module", "uploaded_by")[:RECENT]:
        url = reverse("core:activity_edit", args=[a.pk]) if staff \
            else reverse("core:activity_detail", args=[a.pk])
        rows.append(_item("Activity", a, url, a.updated_at,
                          ("Published" if a.is_published else "Draft") if staff else None, with_who))
    rows.sort(key=lambda r: r["when"], reverse=True)
    return rows[:RECENT]


def _split(qs):
    """published / drafts counts for a notes or activities queryset, in one query."""
    return qs.aggregate(published=Count("pk", filter=Q(is_published=True)),
                        drafts=Count("pk", filter=Q(is_published=False)))


def _assessment_counts(exams=None, attempts=None):
    """open / total exams and submitted attempts still awaiting marking.

    Never raises: if the latest assessments migration has not been applied the
    dashboard must still load, so it quietly shows zeros (as the student page's
    open-assessment list already does).
    """
    from assessments.models import Attempt, Exam

    exams = Exam.objects.all() if exams is None else exams
    attempts = Attempt.objects.all() if attempts is None else attempts
    try:
        counts = exams.aggregate(total=Count("pk"), open=Count("pk", filter=Q(is_open=True)))
        pending = attempts.filter(status=Attempt.SUBMITTED, marking_complete=False).count()
    except DatabaseError:
        return {"total": 0, "open": 0, "pending": 0}
    return {"total": counts["total"], "open": counts["open"], "pending": pending}


def _exam_rows(exams):
    """One row per assessment, newest first, for the dashboards' assessment list.

    Each row carries the two links the dashboard needs: the assessment itself (view /
    manage) and its marking guide. ``missing`` counts written questions that still have
    no marking guide, so a trainer sees at a glance which ones need attention.
    Never raises (same reason as _assessment_counts).
    """
    from assessments.models import Question

    try:
        exams = list(exams.select_related("module", "created_by")
                     .annotate(n_questions=Count("questions", distinct=True))
                     .order_by("-created_at"))
        missing = {}
        for q in Question.objects.filter(exam__in=exams, section="open").select_related("key"):
            key = getattr(q, "key", None)
            if not (key and (key.data or {}).get("guide", "").strip()):
                missing[q.exam_id] = missing.get(q.exam_id, 0) + 1
    except DatabaseError:
        return []
    return [{
        "title": e.title,
        "code": e.module.code if e.module else "",
        "owner": _who(e.created_by),
        "is_open": e.is_open,
        "n_questions": e.n_questions,
        "missing": missing.get(e.pk, 0),
        "detail_url": reverse("assessments:exam_detail", args=[e.pk]),
        "guide_url": reverse("assessments:exam_guide", args=[e.pk]),
    } for e in exams]


# --------------------------------------------------------------------------- #
# Student
# --------------------------------------------------------------------------- #

def _student(request):
    from assessments.dashboard import dashboard_assessments

    levels = []
    for trade in Trade.objects.filter(kind=Trade.KIND_TRADE).prefetch_related("modules"):
        notes, lessons = trade.note_count, trade.lesson_count
        if notes or lessons:        # a level with nothing to open is left out
            levels.append({"trade": trade, "modules": len(trade.modules.all()),
                           "notes": notes, "lessons": lessons})

    latest = _merged(ModuleNote.objects.published().order_by("-published_at"),
                     Activity.objects.filter(is_published=True).order_by("-updated_at"),
                     staff=False)

    open_assessments = dashboard_assessments()
    resume, saved = None, []
    if request.user.is_authenticated:
        mine = (StudentActivity.objects.filter(user=request.user)
                .select_related("lesson", "lesson__unit", "lesson__unit__module")
                .order_by("-last_viewed"))
        resume = next((a for a in mine[:30] if 3 < a.progress_pct < 96), None)
        saved = list(mine.filter(bookmarked=True)[:SAVED])

    return "core/dashboard.html", {
        "levels": levels,
        "latest": latest,
        "open_assessments": open_assessments,
        "new_assessment_count": sum(1 for e in open_assessments if e.is_new),
        "resume": resume,
        "saved": saved,
        "has_content": bool(levels or latest),
    }


# --------------------------------------------------------------------------- #
# Trainer
# --------------------------------------------------------------------------- #

def _trainer(request):
    from assessments.models import Attempt, Exam

    user = request.user
    my_notes = ModuleNote.objects.filter(uploaded_by=user)
    my_acts = Activity.objects.filter(uploaded_by=user)

    profile = getattr(user, "profile", None)
    modules = list(profile.trainer_modules.order_by("code")) if profile else []

    notes, acts = _split(my_notes), _split(my_acts)
    exams = _assessment_counts(Exam.objects.filter(created_by=user),
                               Attempt.objects.filter(exam__created_by=user))

    return "core/dashboard_trainer.html", {
        "stats": [
            _stat("core:notes_manage", "bi-journal-text", "Notes", notes["published"],
                  _plural(notes["drafts"], "draft"), notes["drafts"]),
            _stat("core:activities_manage", "bi-clipboard2-check", "Activities", acts["published"],
                  _plural(acts["drafts"], "draft"), acts["drafts"]),
            _stat("assessments:exam_list", "bi-shield-check", "Open assessments", exams["open"],
                  f"{exams['total']} in total"),
            _stat("assessments:exam_list", "bi-pencil-square", "Awaiting marking", exams["pending"],
                  "submissions to mark" if exams["pending"] else "all marked", exams["pending"]),
        ],
        "exams": _exam_rows(Exam.objects.filter(created_by=user)),
        "recent": _merged(my_notes.order_by("-updated_at"),
                          my_acts.order_by("-updated_at"), staff=True, with_who=False),
        "modules": modules,
    }


# --------------------------------------------------------------------------- #
# Administrator
# --------------------------------------------------------------------------- #

def _admin(request):
    from assessments.models import Exam

    trainers = get_user_model().objects.filter(groups__name=TRAINER, is_active=True).distinct()
    n_trainers = trainers.count()
    unassigned = (trainers.filter(Q(profile__isnull=True) | Q(profile__trainer_modules__isnull=True))
                  .distinct().count())

    notes, acts = _split(ModuleNote.objects.all()), _split(Activity.objects.all())
    assessments = _assessment_counts()
    published = notes["published"] + acts["published"]
    drafts = notes["drafts"] + acts["drafts"]

    # Only things that genuinely need an administrator or trainer to act appear here.
    attention = []
    if unassigned:
        attention.append({
            "icon": "bi-person-exclamation", "tone": "warn",
            "text": f"{_plural(unassigned, 'trainer')} without modules",
            "hint": "Assign modules so they can add notes.",
            "url": reverse("accounts:manage_trainers"), "cta": "Trainers"})
    if assessments["pending"]:
        attention.append({
            "icon": "bi-pencil-square", "tone": "warn",
            "text": f"{_plural(assessments['pending'], 'submission')} awaiting marking",
            "hint": "Open questions are still unmarked.",
            "url": reverse("assessments:exam_list"), "cta": "Assessments"})
    if drafts:
        attention.append({
            "icon": "bi-file-earmark-text", "tone": "",
            "text": f"{_plural(drafts, 'draft')} not yet published",
            "hint": "Notes and activities students cannot see yet.",
            "url": reverse("core:notes_manage"), "cta": "Review"})

    levels = Trade.objects.filter(kind=Trade.KIND_TRADE).count()

    return "core/dashboard_admin.html", {
        "stats": [
            _stat("accounts:manage_trainers", "bi-people", "Trainers", n_trainers,
                  f"{unassigned} without modules" if unassigned else "all have modules", unassigned),
            _stat("core:curriculum", "bi-diagram-3", "Modules", Module.objects.count(),
                  _plural(levels, "level")),
            _stat("core:notes_manage", "bi-journal-text", "Published content", published,
                  _plural(drafts, "draft"), drafts),
            _stat("assessments:exam_list", "bi-shield-check", "Open assessments",
                  assessments["open"], f"{assessments['total']} in total"),
        ],
        "attention": attention,
        "exams": _exam_rows(Exam.objects.all()),
        "recent": _merged(ModuleNote.objects.order_by("-updated_at"),
                          Activity.objects.order_by("-updated_at"), staff=True),
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build(request):
    """Return ``(template_name, context)`` for whoever is looking at the page."""
    if is_admin(request.user):
        return _admin(request)
    if is_trainer(request.user):
        return _trainer(request)
    return _student(request)
