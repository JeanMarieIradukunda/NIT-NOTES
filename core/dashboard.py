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

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from accounts.roles import TRAINER, is_admin, is_trainer
from .models import Activity, Module, ModuleNote, StudentActivity, Trade

RECENT = 5          # rows in any "latest" / "recent" list
SAVED = 3           # saved lessons shown to a signed-in student


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _stat(url_name, icon, label, value, sub, warn=False, tone="blue", *, query="", sub_url="", trend=None):
    """One headline number.

    ``warn`` tints the sub-line amber (only when it is non-zero). ``query`` (e.g. "?status=published")
    makes the card open the matching filtered list; ``sub_url`` turns the sub-line into its own link
    (e.g. "2 drafts" -> the drafts); ``trend`` is a ``_trend()`` chip, or None for no chip.
    """
    return {"url": reverse(url_name) + query, "icon": icon, "label": label,
            "value": value, "sub": sub, "warn": bool(warn), "tone": tone,
            "sub_url": sub_url, "trend": trend}


def _since():
    """The start of the 'this week' window used by the trend chips (the last 7 days)."""
    return timezone.now() - timedelta(days=7)


def _trend(n):
    """A small 'what changed this week' chip for a summary card."""
    if n:
        return {"dir": "up", "icon": "bi-arrow-up-right", "n": n, "text": f"+{n} new this week"}
    return {"dir": "flat", "icon": "bi-dash", "n": 0, "text": "nothing new this week"}


def _new_exams(exams):
    """Assessments created in the last 7 days. Never raises (see _assessment_counts)."""
    try:
        return exams.filter(created_at__gte=_since()).count()
    except DatabaseError:
        return 0


def _act(label, hint, icon, tone, url_name, badge=0):
    """A quick-action button on a staff dashboard. ``tone`` picks its colour (see dashboard.css)."""
    return {"label": label, "hint": hint, "icon": icon, "tone": tone,
            "url": reverse(url_name), "badge": badge}


def _attn(icon, tone, text, hint, url, cta):
    """One row of the "needs attention" panel: what is wrong, why it matters, one button to fix it."""
    return {"icon": icon, "tone": tone, "text": text, "hint": hint, "url": url, "cta": cta}


def _who(user):
    return (user.first_name or user.username) if user else ""


def _item(kind, obj, url, when, status=None, with_who=True):
    """One row in a feed. ``status`` ("Published"/"Draft") is only set for staff lists."""
    return {
        "kind": kind,                       # "Notes" | "Activity"
        "tone": "blue" if kind == "Notes" else "teal",
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
    from assessments.models import Attempt, Question

    try:
        exams = list(exams.select_related("module", "created_by")
                     .annotate(n_questions=Count("questions", distinct=True),
                               n_attempts=Count("attempts", distinct=True),
                               n_submitted=Count("attempts", filter=Q(attempts__status=Attempt.SUBMITTED),
                                                 distinct=True))
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
        "edit_guides_url": reverse("assessments:guides_edit", args=[e.pk]),
        "n_attempts": e.n_attempts,
        "n_submitted": e.n_submitted,
        "results_url": reverse("assessments:exam_results", args=[e.pk]),
        "clear_url": reverse("assessments:exam_clear_submissions", args=[e.pk]),
    } for e in exams]


WEEKS = 8


def _weekly_submissions():
    """Submitted attempts per week for the last WEEKS weeks (oldest first), for the admin chart."""
    from datetime import datetime, time, timedelta

    from django.db.models.functions import TruncWeek
    from django.utils import timezone

    from assessments.models import Attempt

    this_monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
    mondays = [this_monday - timedelta(weeks=i) for i in range(WEEKS - 1, -1, -1)]
    start = timezone.make_aware(datetime.combine(mondays[0], time.min))
    try:
        found = {}
        for row in (Attempt.objects.filter(status=Attempt.SUBMITTED, submitted_at__gte=start)
                    .annotate(week=TruncWeek("submitted_at")).values("week").annotate(n=Count("pk"))):
            day = row["week"].date() if hasattr(row["week"], "date") else row["week"]
            found[day] = row["n"]
    except DatabaseError:
        found = {}
    counts = [found.get(m, 0) for m in mondays]
    peak = max(counts) or 1
    return {
        "bars": [{"label": m.strftime("%d %b").lstrip("0"), "count": c,
                  "pct": (max(round(c / peak * 100), 6) if c else 0)} for m, c in zip(mondays, counts)],
        "total": sum(counts), "this_week": counts[-1],
    }


# --------------------------------------------------------------------------- #
# Student
# --------------------------------------------------------------------------- #

def _student(request):
    from assessments.dashboard import dashboard_assessments

    levels = []
    for trade in Trade.objects.filter(kind=Trade.KIND_TRADE).prefetch_related("modules"):
        notes, lessons = trade.note_count, trade.lesson_count
        if notes or lessons:        # a level with nothing to open is left out
            digits = re.search(r"\d+", trade.name)
            levels.append({"trade": trade, "modules": len(trade.modules.all()),
                           "notes": notes, "lessons": lessons,
                           "number": digits.group() if digits else trade.name[:1].upper(),
                           "tone": ("blue", "teal", "violet")[len(levels) % 3]})

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
    exam_rows = _exam_rows(Exam.objects.filter(created_by=user))

    # What needs doing, most urgent first. Empty list means "all caught up".
    attention = []
    if exams["pending"]:
        attention.append(_attn(
            "bi-pencil-square", "amber", f"{_plural(exams['pending'], 'submission')} waiting to be marked",
            "Candidates are waiting for their written answers to be marked.",
            reverse("assessments:marking_queue"), "Open marking queue"))
    drafts = notes["drafts"] + acts["drafts"]
    if drafts:
        attention.append(_attn(
            "bi-file-earmark-text", "blue", f"{_plural(drafts, 'draft')} not yet published",
            "Students cannot see these until you publish them.",
            reverse("core:notes_manage" if notes["drafts"] else "core:activities_manage"), "Review drafts"))
    no_guide = [r for r in exam_rows if r["missing"]]
    if no_guide:
        total = sum(r["missing"] for r in no_guide)
        one = len(no_guide) == 1
        attention.append(_attn(
            "bi-journal-check", "violet", f"{_plural(total, 'written question')} without a marking guide",
            f"In {no_guide[0]['title']}." if one else f"Across {len(no_guide)} assessments.",
            no_guide[0]["edit_guides_url"] if one else reverse("assessments:exam_list"), "Add guides"))

    return "core/dashboard_trainer.html", {
        "attention": attention,
        "actions": [
            _act("Add notes", "Upload a PDF or HTML file", "bi-journal-plus", "blue", "core:note_create"),
            _act("Add activity", "A worksheet or practice task", "bi-clipboard2-plus", "teal", "core:activity_create"),
            _act("New assessment", "Timed, with an exam password", "bi-shield-plus", "violet", "assessments:exam_create"),
            _act("Marking queue", f"{exams['pending']} waiting" if exams["pending"] else "Nothing waiting",
                 "bi-pencil-square", "amber", "assessments:marking_queue", exams["pending"]),
        ],
        "stats": [
            _stat("core:notes_manage", "bi-journal-text", "Notes published", notes["published"],
                  _plural(notes["drafts"], "draft"), notes["drafts"], "blue", query="?status=published",
                  sub_url=reverse("core:notes_manage") + "?status=draft" if notes["drafts"] else "",
                  trend=_trend(my_notes.filter(is_published=True, published_at__gte=_since()).count())),
            _stat("core:activities_manage", "bi-clipboard2-check", "Activities published", acts["published"],
                  _plural(acts["drafts"], "draft"), acts["drafts"], "teal", query="?status=published",
                  sub_url=reverse("core:activities_manage") + "?status=draft" if acts["drafts"] else "",
                  trend=_trend(my_acts.filter(is_published=True, created_at__gte=_since()).count())),
            _stat("assessments:exam_list", "bi-shield-check", "Assessments open", exams["open"],
                  f"{exams['total']} in total", False, "violet",
                  trend=_trend(_new_exams(Exam.objects.filter(created_by=user)))),
        ],
        "exams": exam_rows,
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
        attention.append(_attn(
            "bi-person-exclamation", "amber", f"{_plural(unassigned, 'trainer')} without modules",
            "Assign modules so they can add notes.", reverse("accounts:manage_trainers"), "Assign modules"))
    if assessments["pending"]:
        attention.append(_attn(
            "bi-pencil-square", "amber", f"{_plural(assessments['pending'], 'submission')} awaiting marking",
            "Written answers are still unmarked.", reverse("assessments:marking_queue"), "Open marking queue"))
    if drafts:
        attention.append(_attn(
            "bi-file-earmark-text", "blue", f"{_plural(drafts, 'draft')} not yet published",
            "Notes and activities students cannot see yet.", reverse("core:notes_manage"), "Review drafts"))

    levels = Trade.objects.filter(kind=Trade.KIND_TRADE).count()

    return "core/dashboard_admin.html", {
        "attention": attention,
        "actions": [
            _act("Add trainer", "Create a trainer account", "bi-person-plus", "blue", "accounts:create_trainer"),
            _act("Curriculum", "Levels, modules and units", "bi-diagram-3", "teal", "core:curriculum"),
            _act("All assessments", "Every trainer's assessments", "bi-shield-check", "violet", "assessments:exam_list"),
            _act("Marking queue", f"{assessments['pending']} waiting" if assessments["pending"] else "Nothing waiting",
                 "bi-pencil-square", "amber", "assessments:marking_queue", assessments["pending"]),
        ],
        "stats": [
            _stat("accounts:manage_trainers", "bi-people", "Trainers", n_trainers,
                  f"{unassigned} without modules" if unassigned else "all have modules", unassigned, "blue",
                  trend=_trend(trainers.filter(date_joined__gte=_since()).count())),
            _stat("core:curriculum", "bi-diagram-3", "Modules", Module.objects.count(),
                  _plural(levels, "level"), False, "teal"),
            _stat("core:notes_manage", "bi-journal-text", "Published content", published,
                  _plural(drafts, "draft"), drafts, "blue", query="?status=published",
                  sub_url=(reverse("core:notes_manage" if notes["drafts"] else "core:activities_manage")
                           + "?status=draft") if drafts else "",
                  trend=_trend(ModuleNote.objects.filter(is_published=True, published_at__gte=_since()).count()
                               + Activity.objects.filter(is_published=True, created_at__gte=_since()).count())),
            _stat("assessments:exam_list", "bi-shield-check", "Assessments open",
                  assessments["open"], f"{assessments['total']} in total", False, "violet",
                  trend=_trend(_new_exams(Exam.objects.all()))),
        ],
        "weekly": _weekly_submissions(),
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
