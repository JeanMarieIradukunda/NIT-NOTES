"""
core.views
===========

Everything a student, trainer or administrator touches day to day. Kept as
plain function views — the platform is small enough that class-based views
would add indirection without buying anything back.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.roles import can_add_notes, can_manage, can_upload, is_admin
from .forms import LessonUploadForm
from .html_processing import LessonContentError, process_lesson_html
from .models import (Lesson, LessonUpload, Module, ModuleNote, Resource,
                     StudentActivity, Trade, Unit)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _activity_for(user, lesson):
    if not user.is_authenticated:
        return None
    obj, _ = StudentActivity.objects.get_or_create(user=user, lesson=lesson)
    return obj


def _library_stats():
    return {
        "modules": Module.objects.count(),
        "notes": ModuleNote.objects.published().count(),
        "lessons": Lesson.objects.filter(is_published=True).count(),
        "resources": Resource.objects.count(),
        "minutes": Lesson.objects.filter(is_published=True).aggregate(
            m=Sum("minutes"))["m"] or 0,
    }


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

def dashboard(request):
    trades = (Trade.objects.filter(kind=Trade.KIND_TRADE)
              .prefetch_related("modules"))
    stats = _library_stats()

    resume = None
    recents = []
    bookmarks = []
    if request.user.is_authenticated:
        qs = (StudentActivity.objects.filter(user=request.user)
              .select_related("lesson", "lesson__unit", "lesson__unit__module",
                              "lesson__unit__module__trade")
              .order_by("-last_viewed"))
        recents = list(qs[:6])
        resume = next((a for a in qs if 3 < a.progress_pct < 96), None)
        bookmarks = list(qs.filter(bookmarked=True)[:6])

    latest_notes = list(ModuleNote.objects.published()
                        .select_related("module", "module__trade", "uploaded_by")
                        .order_by("-published_at")[:6])

    my_notes = None
    if can_add_notes(request.user):
        mine = ModuleNote.objects.all() if is_admin(request.user) \
            else ModuleNote.objects.filter(uploaded_by=request.user)
        my_notes = {"total": mine.count(),
                    "published": mine.filter(is_published=True).count(),
                    "drafts": mine.filter(is_published=False).count()}

    return render(request, "core/dashboard.html", {
        "trades": trades, "stats": stats, "latest_notes": latest_notes,
        "my_notes": my_notes,
        "resume": resume, "recents": recents, "bookmarks": bookmarks,
    })


# --------------------------------------------------------------------------- #
# Browse
# --------------------------------------------------------------------------- #

def browse(request):
    trade_key = request.GET.get("trade", "").strip()
    trades = Trade.objects.prefetch_related("modules")
    selected = None

    if trade_key:
        selected = trades.filter(key=trade_key).first()
        if not selected:
            return render(request, "core/error_panel.html", {
                "title": "That level does not exist",
                "detail": "Check the address, or browse everything available instead.",
            }, status=404)
        trades = [selected]

    # Only modules with something students can open are listed, and levels with
    # no such module are left out — so an empty library shows one clear message
    # rather than a page of bare headings.
    sections = []
    for trade in trades:
        modules = []
        for module in trade.modules.all():
            notes = module.published_note_count
            lessons = module.lesson_count
            downloads = module.resources.count()
            if notes or lessons or downloads:
                modules.append({"module": module, "notes": notes,
                                "lessons": lessons, "downloads": downloads})
        if modules:
            sections.append({"trade": trade, "modules": modules})

    return render(request, "core/browse.html", {"sections": sections, "selected": selected})


# --------------------------------------------------------------------------- #
# Module
# --------------------------------------------------------------------------- #

def module_detail(request, trade_key, module_key):
    module = Module.objects.filter(trade__key=trade_key, key=module_key) \
        .select_related("trade").first()
    if not module:
        return render(request, "core/error_panel.html", {
            "title": "Module not found",
            "detail": "This module may have been renamed. Browse the levels to find it.",
        }, status=404)

    units = (module.units
             .prefetch_related("lessons")
             .order_by("order", "code"))
    resources = module.resources.exclude(
        kind__in=Resource.ASSESSMENT_KINDS).order_by("order", "name")
    assessments = module.resources.filter(
        kind__in=Resource.ASSESSMENT_KINDS).order_by("order", "name")

    activity_map = {}
    if request.user.is_authenticated:
        activity_map = {
            a.lesson_id: a for a in
            StudentActivity.objects.filter(user=request.user, lesson__unit__module=module)
        }

    notes = (module.notes.published()
             .select_related("uploaded_by").order_by("-published_at", "title"))

    return render(request, "core/module_detail.html", {
        "module": module, "units": units, "resources": resources,
        "assessments": assessments, "activity_map": activity_map,
        "notes": notes,
        "can_upload_here": can_manage(request.user, module),
        "can_add_notes_here": can_add_notes(request.user),
    })


# --------------------------------------------------------------------------- #
# Lesson reader
# --------------------------------------------------------------------------- #

def lesson_detail(request, slug):
    lesson = (Lesson.objects.select_related(
        "unit", "unit__module", "unit__module__trade")
        .filter(slug=slug, is_published=True).first())
    if not lesson:
        return render(request, "core/error_panel.html", {
            "title": "Lesson not found",
            "detail": "This lesson may have been renamed or moved. Search the library to find it.",
        }, status=404)

    prev_lesson, next_lesson = lesson.neighbours()
    activity = _activity_for(request.user, lesson)
    if activity:
        activity.view_count += 1
        activity.save(update_fields=["view_count", "last_viewed"])

    siblings = list(lesson.unit.module.lessons().order_by("order", "title"))
    position = next((i + 1 for i, l in enumerate(siblings) if l.pk == lesson.pk), None)

    return render(request, "core/lesson_detail.html", {
        "lesson": lesson, "prev_lesson": prev_lesson, "next_lesson": next_lesson,
        "activity": activity, "position": position, "sibling_count": len(siblings),
        "ai_enabled": bool(settings.GROQ_API_KEY),
    })


@login_required
@require_POST
def update_activity(request, slug):
    """AJAX endpoint the reader calls to persist progress, bookmark and completion."""
    lesson = get_object_or_404(Lesson, slug=slug)
    activity, _ = StudentActivity.objects.get_or_create(user=request.user, lesson=lesson)

    action = request.POST.get("action")
    if action == "progress":
        pct = max(0, min(100, int(request.POST.get("pct", 0) or 0)))
        if pct > activity.progress_pct:
            activity.progress_pct = pct
            if pct >= 96:
                activity.completed = True
    elif action == "toggle_bookmark":
        activity.bookmarked = not activity.bookmarked
    elif action == "toggle_complete":
        activity.completed = not activity.completed
        if activity.completed:
            activity.progress_pct = 100

    activity.save()
    return JsonResponse({
        "bookmarked": activity.bookmarked,
        "completed": activity.completed,
        "progress_pct": activity.progress_pct,
    })


# --------------------------------------------------------------------------- #
# PDF export — server-side, via WeasyPrint. Real page boxes, real headers and
# footers via CSS paged media, so the text stays selectable — no screenshot.
# --------------------------------------------------------------------------- #

def lesson_pdf(request, slug):
    lesson = get_object_or_404(
        Lesson.objects.select_related("unit", "unit__module", "unit__module__trade"),
        slug=slug, is_published=True)

    try:
        from weasyprint import HTML
    except Exception:
        return render(request, "core/error_panel.html", {
            "title": "PDF export is not available on this server",
            "detail": ("WeasyPrint could not be loaded. Ask your administrator to check "
                       "it is installed (`pip install weasyprint`) along with its Pango "
                       "and Cairo system libraries — see the README."),
        }, status=503)

    html_string = render_to_string("core/lesson_pdf.html", {
        "lesson": lesson,
        "brand": {
            "institution": "Padri Vjeko Centre TSS",
            "platform": "NIT Learning Resources",
            "department": "Department of Information Technology",
        },
        "generated": timezone.now(),
    })

    pdf_bytes = HTML(string=html_string, base_url=request.build_absolute_uri("/")).write_pdf()

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{lesson.pdf_filename()}"'
    return response


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def search(request):
    query = request.GET.get("q", "").strip()
    results, note_results = [], []

    if len(query) >= 2:
        terms = query.split()
        q = Q()
        nq = Q()
        for term in terms:
            q &= (Q(title__icontains=term) | Q(search_text__icontains=term) |
                  Q(unit__module__code__icontains=term) |
                  Q(unit__module__name__icontains=term) |
                  Q(unit__title__icontains=term))
            nq &= (Q(title__icontains=term) | Q(search_text__icontains=term) |
                   Q(module__code__icontains=term) | Q(module__name__icontains=term))
        results = (Lesson.objects.filter(q, is_published=True)
                   .select_related("unit", "unit__module", "unit__module__trade")
                   .distinct()[:60])
        note_results = list(ModuleNote.objects.published().filter(nq)
                            .select_related("module", "module__trade")
                            .distinct().order_by("-published_at")[:60])

    return render(request, "core/search.html", {
        "query": query, "results": results, "note_results": note_results,
        "total": len(results) + len(note_results)})


# --------------------------------------------------------------------------- #
# Upload — the "add new notes here" feature requested per level/module.
# --------------------------------------------------------------------------- #

@login_required
@user_passes_test(can_upload, login_url="accounts:login")
def upload_lesson(request, trade_key, module_key):
    module = get_object_or_404(Module.objects.select_related("trade"),
                                trade__key=trade_key, key=module_key)

    # A Trainer may only be an Administrator or a Trainer assigned to this
    # specific module — never another Trainer's module.
    if not can_manage(request.user, module):
        return render(request, "core/error_panel.html", {
            "title": "You don't have access to this module",
            "detail": ("You're signed in as a Trainer, but this module isn't assigned "
                       "to you. Ask an administrator to assign it to your account if "
                       "you need to manage it."),
        }, status=403)

    if request.method == "POST":
        form = LessonUploadForm(request.POST, request.FILES, module=module)
        if form.is_valid():
            upload = LessonUpload(
                module=module,
                unit=form.cleaned_data.get("unit"),
                new_unit_title=form.cleaned_data.get("new_unit_title", ""),
                source_html=form.cleaned_data["source_html"],
                original_filename=form.cleaned_data["source_html"].name,
                uploaded_by=request.user,
            )
            upload.save()

            try:
                upload.source_html.open("rb")
                raw = upload.source_html.read().decode("utf-8", "replace")
            finally:
                upload.source_html.close()

            try:
                parsed = process_lesson_html(
                    raw, max_bytes=settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024)
            except LessonContentError as exc:
                upload.status = LessonUpload.STATUS_REJECTED
                upload.note = str(exc)
                upload.save(update_fields=["status", "note"])
                messages.error(request, f"Upload rejected: {exc}")
                return redirect("core:upload_lesson", trade_key=trade_key, module_key=module_key)

            unit = form.cleaned_data.get("unit")
            if not unit:
                last_order = module.units.count() + 1
                unit = Unit.objects.create(
                    module=module,
                    code=f"U{last_order}",
                    title=form.cleaned_data["new_unit_title"].strip(),
                    kind=Unit.KIND_TOPIC,
                    order=last_order,
                )

            lesson = Lesson.objects.create(
                unit=unit,
                title=parsed["title"],
                content_html=parsed["fragment"],
                words=parsed["words"],
                minutes=parsed["minutes"],
                tables=parsed["tables"],
                images=parsed["images"],
                code_blocks=parsed["code_blocks"],
                headings_json=parsed["headings"],
                search_text=parsed["text"],
                order=form.cleaned_data.get("order") or 99,
                uploaded_by=request.user,
                source_file=f"upload:{upload.original_filename}",
            )
            upload.lesson = lesson
            upload.status = LessonUpload.STATUS_PUBLISHED
            upload.save(update_fields=["lesson", "status"])

            messages.success(
                request,
                f"“{lesson.title}” was published to {module.code}. "
                "Students can open, search for and download it immediately.")
            return redirect("core:lesson_detail", slug=lesson.slug)
    else:
        form = LessonUploadForm(module=module)

    recent_uploads = module.uploads.select_related("lesson")[:8]

    return render(request, "core/upload_lesson.html", {
        "module": module, "form": form, "recent_uploads": recent_uploads,
        "MAX_UPLOAD_SIZE_MB": settings.MAX_UPLOAD_SIZE_MB,
    })
