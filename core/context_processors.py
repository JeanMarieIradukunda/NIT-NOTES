def brand(request):
    """Institution branding, available in every template without a view passing it."""
    return {
        "BRAND": {
            "institution": "Padri Vjeko Centre TSS",
            "platform": "NIT Learning Resources",
            "department": "Department of Information Technology",
        }
    }


def admin_dashboard(request):
    """
    Live stats, quick-add links, and recent items for the Django admin index
    page only. Runs nowhere else, so it costs nothing on the rest of the site.
    """
    match = getattr(request, "resolver_match", None)
    if not match or match.view_name != "admin:index":
        return {}

    from django.contrib.auth import get_user_model
    from django.db.utils import OperationalError, ProgrammingError
    from django.urls import reverse

    from core.models import Lesson, LessonUpload, Module, ModuleNote, Resource, Trade

    try:
        stats = [
            {"label": "Trades", "value": Trade.objects.count(), "icon": "bi-signpost-2",
             "url": reverse("admin:core_trade_changelist")},
            {"label": "Modules", "value": Module.objects.count(), "icon": "bi-diagram-3",
             "url": reverse("admin:core_module_changelist")},
            {"label": "Lessons", "value": Lesson.objects.count(), "icon": "bi-journal-text",
             "sub": f"{Lesson.objects.filter(is_published=True).count()} published",
             "url": reverse("admin:core_lesson_changelist")},
            {"label": "Module notes", "value": ModuleNote.objects.count(), "icon": "bi-file-earmark-text",
             "sub": f"{ModuleNote.objects.filter(is_published=True).count()} published",
             "url": reverse("admin:core_modulenote_changelist")},
            {"label": "Resources", "value": Resource.objects.count(), "icon": "bi-folder2-open",
             "url": reverse("admin:core_resource_changelist")},
            {"label": "Users", "value": get_user_model().objects.count(), "icon": "bi-people",
             "url": reverse("admin:auth_user_changelist")},
        ]
        pending_uploads = list(
            LessonUpload.objects.filter(status=LessonUpload.STATUS_PENDING)
            .select_related("module", "uploaded_by").order_by("-created_at")[:6]
        )
        recent_lessons = list(
            Lesson.objects.select_related("unit__module").order_by("-updated_at")[:6]
        )
        recent_notes = list(
            ModuleNote.objects.select_related("module").order_by("-updated_at")[:6]
        )
    except (OperationalError, ProgrammingError):
        # Tables not migrated yet — let the plain admin index render.
        return {}

    return {
        "nit_dashboard_stats": stats,
        "nit_pending_uploads": pending_uploads,
        "nit_recent_lessons": recent_lessons,
        "nit_recent_notes": recent_notes,
    }
