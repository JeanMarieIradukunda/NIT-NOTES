"""
core.activity_views
====================

Activities: the Trainer-facing workspace (add, edit, replace, publish,
unpublish, delete) — the app-level replacement for creating/managing
Activity rows in Django Admin. Only Module, Topic and the document are ever
required (see core.forms.ActivityForm).

Access rules mirror module notes exactly:

* Adding activities ....... any Trainer or Administrator
* Editing / publishing /
  unpublishing / deleting .. the Trainer who added the activity, or any
                              Administrator
* Viewing / downloading the
  document .................. anyone if published; its author and
                               Administrators only if a draft
"""

from functools import wraps

import re
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import render, redirect
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods, require_POST

from accounts.roles import (activity_modules_for, can_add_activities,
                            can_manage_activity, is_admin)
from .forms import ActivityForm
from .models import Activity, Module

PAGE_SIZE = 15


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _forbidden(request, title, detail):
    return render(request, "core/error_panel.html",
                  {"title": title, "detail": detail}, status=403)


def _not_found(request):
    return render(request, "core/error_panel.html", {
        "title": "Activity not found",
        "detail": "This activity may have been unpublished, moved or deleted.",
    }, status=404)


def trainer_required(view):
    """Signed-in Trainers/Administrators only. Others get a clear 403, not a login loop."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not can_add_activities(request.user):
            return _forbidden(request, "Trainers only",
                              "Adding and managing activities is available to Trainers "
                              "and Administrators. Published activities are open to "
                              "everyone.")
        return view(request, *args, **kwargs)
    return wrapper


def _activity_qs():
    return Activity.objects.select_related("module", "module__trade", "topic", "uploaded_by")


def _visible_activity_or_none(request, pk):
    """A published activity, or a draft the current user is allowed to manage."""
    activity = _activity_qs().filter(pk=pk).first()
    if activity is None:
        return None
    if activity.is_published or can_manage_activity(request.user, activity):
        return activity
    return None


def _manageable_activity_or_response(request, pk):
    """Returns (activity, None) if the user may manage it, else (None, response)."""
    activity = _activity_qs().filter(pk=pk).first()
    if activity is None:
        return None, _not_found(request)
    if not can_manage_activity(request.user, activity):
        return None, _forbidden(
            request, "You can only manage your own activities",
            "This activity was added by another Trainer. Ask them, or an "
            "administrator, if something needs changing.")
    return activity, None


def _safe_next(request):
    """A same-site `next` URL from the request, or None (guards against open redirects)."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return None


# --------------------------------------------------------------------------- #
# Trainer workspace
# --------------------------------------------------------------------------- #

@trainer_required
def activities_manage(request):
    base = _activity_qs()
    if not is_admin(request.user):
        base = base.filter(uploaded_by=request.user)

    status = request.GET.get("status", "all")
    module_id = request.GET.get("module", "").strip()
    query = request.GET.get("q", "").strip()

    counts = {
        "all": base.count(),
        "published": base.filter(is_published=True).count(),
        "draft": base.filter(is_published=False).count(),
    }

    activities = base
    if status == "published":
        activities = activities.filter(is_published=True)
    elif status == "draft":
        activities = activities.filter(is_published=False)
    else:
        status = "all"
    if module_id.isdigit():
        activities = activities.filter(module_id=int(module_id))
    if query:
        for term in query.split():
            activities = activities.filter(
                Q(title__icontains=term) | Q(original_filename__icontains=term) |
                Q(module__code__icontains=term) | Q(module__name__icontains=term) |
                Q(topic__title__icontains=term))

    filter_modules = (Module.objects.filter(activities__in=base).distinct()
                      .select_related("trade").order_by("code"))

    page = Paginator(activities, PAGE_SIZE).get_page(request.GET.get("page"))
    qs = request.GET.copy()
    qs.pop("page", None)

    return render(request, "core/activities_manage.html", {
        "page": page, "counts": counts, "status": status, "query": query,
        "module_id": module_id, "filter_modules": filter_modules,
        "querystring": qs.urlencode(), "show_owner": is_admin(request.user),
        "has_filters": bool(query or module_id or status != "all"),
    })


@trainer_required
@require_http_methods(["GET", "POST"])
def activity_create(request):
    if request.method == "POST":
        form = ActivityForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            intent = "publish" if request.POST.get("intent") == "publish" else "draft"
            activity = form.apply(intent)
            if activity.is_published:
                messages.success(request, format_html(
                    "“{}” is published in {}. Students can open it now.",
                    activity.title, activity.module.code))
            else:
                messages.success(request, format_html(
                    "“{}” was saved as a draft in {}. Only you and administrators "
                    "can see it until you publish it.", activity.title, activity.module.code))
            return redirect("core:activities_manage")
    else:
        initial = {}
        module_id = request.GET.get("module", "").strip()
        if module_id.isdigit() and activity_modules_for(request.user).filter(pk=int(module_id)).exists():
            initial["module"] = int(module_id)
        form = ActivityForm(user=request.user, initial=initial)

    return render(request, "core/activity_form.html", {
        "form": form, "activity": None,
        "max_mb": settings.MAX_ACTIVITY_SIZE_MB,
        "has_modules": form.fields["module"].queryset.exists(),
    })


@trainer_required
@require_http_methods(["GET", "POST"])
def activity_edit(request, pk):
    activity, denied = _manageable_activity_or_response(request, pk)
    if denied:
        return denied

    if request.method == "POST":
        form = ActivityForm(request.POST, request.FILES, user=request.user, activity=activity)
        if form.is_valid():
            intent = request.POST.get("intent")
            if intent not in ("publish", "draft"):
                intent = "save"
            was_published = activity.is_published
            activity = form.apply(intent)
            if activity.is_published and not was_published:
                msg = f"“{activity.title}” is now published."
            elif was_published and not activity.is_published:
                msg = f"“{activity.title}” was unpublished. It's hidden from students but kept as a draft."
            else:
                msg = f"“{activity.title}” was updated."
            messages.success(request, msg)
            return redirect("core:activities_manage")
    else:
        form = ActivityForm(user=request.user, activity=activity, initial={
            "module": activity.module_id, "topic": activity.topic_id, "title": activity.title})

    return render(request, "core/activity_form.html", {
        "form": form, "activity": activity, "max_mb": settings.MAX_ACTIVITY_SIZE_MB,
        "has_modules": True,
    })


@trainer_required
@require_POST
def activity_toggle_publish(request, pk):
    activity, denied = _manageable_activity_or_response(request, pk)
    if denied:
        return denied
    if activity.is_published:
        activity.is_published = False
        messages.success(request, f"“{activity.title}” was unpublished. Students can no longer see it.")
    else:
        activity.is_published = True
        messages.success(request, f"“{activity.title}” is now published.")
    activity.save(update_fields=["is_published", "updated_at"])
    return redirect(_safe_next(request) or "core:activities_manage")


@trainer_required
@require_http_methods(["GET", "POST"])
def activity_delete(request, pk):
    activity, denied = _manageable_activity_or_response(request, pk)
    if denied:
        return denied
    if request.method == "POST":
        title = activity.title
        activity.delete()  # the post_delete signal removes the file from disk
        messages.success(request, f"“{title}” was deleted.")
        return redirect("core:activities_manage")
    return render(request, "core/activity_confirm_delete.html", {"activity": activity})


# --------------------------------------------------------------------------- #
# Serving the document
# --------------------------------------------------------------------------- #

@xframe_options_sameorigin
def activity_file(request, pk):
    """
    Deliver an activity's document. PDFs open inline (or download with
    ?download=1); HTML is only ever offered as a download — never rendered
    as a live page from our own origin.
    """
    activity = _visible_activity_or_none(request, pk)
    if activity is None:
        return _not_found(request)

    try:
        handle = activity.document.storage.open(activity.document.name, "rb")
    except (FileNotFoundError, ValueError):
        return render(request, "core/error_panel.html", {
            "title": "This file is unavailable",
            "detail": "The document for this activity is missing from the server. "
                      "Please tell the Trainer who added it, or an administrator.",
        }, status=404)

    stem = activity.title or activity.original_filename or "activity"
    stem = re.sub(r"[^\w\s-]", "", stem).strip()
    stem = re.sub(r"\s+", "_", stem)[:110] or "activity"
    filename = f"{stem}.{'pdf' if activity.is_pdf else 'html'}"
    as_attachment = activity.is_html or request.GET.get("download") == "1"
    response = FileResponse(
        handle, as_attachment=as_attachment, filename=filename,
        content_type="application/pdf" if activity.is_pdf else "text/html; charset=utf-8")
    response["X-Content-Type-Options"] = "nosniff"
    if activity.is_html:
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cache-Control"] = ("private, max-age=300" if activity.is_published
                                 else "private, no-store")
    return response
