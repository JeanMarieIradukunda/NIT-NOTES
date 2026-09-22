"""
core.note_views
================

Module notes: the Trainer-facing workspace (add, edit, replace, publish,
unpublish, delete) and the student-facing reader for published notes.

Access rules, in one place:

* Adding notes ............ any Trainer or Administrator
* Editing / publishing /
  unpublishing / deleting .. the Trainer who added the note, or any Administrator
* Reading a *published* note or downloading its file ... anyone, no account needed
* Reading a *draft* ....... its author and Administrators only (everyone else
                            gets a 404, so a draft's existence isn't revealed)

Note files are stored outside MEDIA_ROOT and only ever leave the server through
`note_file`, which applies those rules on every request.
"""

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods, require_POST

from accounts.roles import (can_add_notes, can_manage_note, is_admin,
                            note_modules_for)
from .forms import ModuleNoteForm
from .models import Module, ModuleNote

PAGE_SIZE = 15

# Applied to the note reader. Note HTML is sanitised on upload, but this makes
# the reader safe even if a payload ever slipped through: no inline script, no
# javascript: URLs, no plugins, no foreign frames or form targets.
READER_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "frame-src 'self'; frame-ancestors 'self'; form-action 'self'"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _forbidden(request, title, detail):
    return render(request, "core/error_panel.html",
                  {"title": title, "detail": detail}, status=403)


def _not_found(request):
    return render(request, "core/error_panel.html", {
        "title": "Notes not found",
        "detail": "These notes may have been unpublished, moved or deleted.",
    }, status=404)


def trainer_required(view):
    """Signed-in Trainers/Administrators only. Others get a clear 403, not a login loop."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not can_add_notes(request.user):
            return _forbidden(request, "Trainers only",
                              "Adding and managing module notes is available to Trainers "
                              "and Administrators. Published notes are open to everyone.")
        return view(request, *args, **kwargs)
    return wrapper


def _note_qs():
    return ModuleNote.objects.select_related("module", "module__trade", "uploaded_by")


def _visible_note_or_none(request, pk):
    """A published note, or a draft the current user is allowed to manage."""
    note = _note_qs().filter(pk=pk).first()
    if note is None:
        return None
    if note.is_published or can_manage_note(request.user, note):
        return note
    return None


def _manageable_note_or_response(request, pk):
    """Returns (note, None) if the user may manage it, else (None, response)."""
    note = _note_qs().filter(pk=pk).first()
    if note is None:
        return None, _not_found(request)
    if not can_manage_note(request.user, note):
        return None, _forbidden(
            request, "You can only manage your own notes",
            "These notes were added by another Trainer. Ask them, or an administrator, "
            "if something needs changing.")
    return note, None


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
def notes_manage(request):
    base = _note_qs()
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

    notes = base
    if status == "published":
        notes = notes.filter(is_published=True)
    elif status == "draft":
        notes = notes.filter(is_published=False)
    else:
        status = "all"
    if module_id.isdigit():
        notes = notes.filter(module_id=int(module_id))
    if query:
        for term in query.split():
            notes = notes.filter(Q(title__icontains=term) | Q(original_filename__icontains=term) |
                                 Q(module__code__icontains=term) | Q(module__name__icontains=term))

    filter_modules = (Module.objects.filter(notes__in=base).distinct()
                      .select_related("trade").order_by("code"))

    page = Paginator(notes, PAGE_SIZE).get_page(request.GET.get("page"))
    qs = request.GET.copy()
    qs.pop("page", None)

    return render(request, "core/notes_manage.html", {
        "page": page, "counts": counts, "status": status, "query": query,
        "module_id": module_id, "filter_modules": filter_modules,
        "querystring": qs.urlencode(), "show_owner": is_admin(request.user),
        "has_filters": bool(query or module_id or status != "all"),
    })


@trainer_required
@require_http_methods(["GET", "POST"])
def note_create(request):
    if request.method == "POST":
        form = ModuleNoteForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            intent = "publish" if request.POST.get("intent") == "publish" else "draft"
            note = form.apply(intent)
            if note.is_published:
                messages.success(request, format_html(
                    "“{}” is published in {}. Students can open it now. <a href=\"{}\">View notes</a>",
                    note.title, note.module.code, note.get_absolute_url()))
            else:
                messages.success(request, format_html(
                    "“{}” was saved as a draft in {}. Only you and administrators can see it "
                    "until you publish it.", note.title, note.module.code))
            return redirect("core:notes_manage")
    else:
        initial = {}
        module_id = request.GET.get("module", "")
        if module_id.isdigit() and note_modules_for(request.user).filter(pk=int(module_id)).exists():
            initial["module"] = int(module_id)
        form = ModuleNoteForm(user=request.user, initial=initial)

    return render(request, "core/note_form.html", {
        "form": form, "note": None,
        "max_mb": settings.MAX_NOTE_SIZE_MB,
        "has_modules": form.fields["module"].queryset.exists(),
    })


@trainer_required
@require_http_methods(["GET", "POST"])
def note_edit(request, pk):
    note, denied = _manageable_note_or_response(request, pk)
    if denied:
        return denied

    if request.method == "POST":
        form = ModuleNoteForm(request.POST, request.FILES, user=request.user, note=note)
        if form.is_valid():
            intent = request.POST.get("intent")
            if intent not in ("publish", "draft"):
                intent = "save"
            was_published = note.is_published
            note = form.apply(intent)
            if note.is_published and not was_published:
                msg = f"“{note.title}” is now published."
            elif was_published and not note.is_published:
                msg = f"“{note.title}” was unpublished. It's hidden from students but kept as a draft."
            else:
                msg = f"“{note.title}” was updated."
            messages.success(request, msg)
            return redirect("core:notes_manage")
    else:
        form = ModuleNoteForm(user=request.user, note=note, initial={
            "module": note.module_id, "title": note.title})

    return render(request, "core/note_form.html", {
        "form": form, "note": note, "max_mb": settings.MAX_NOTE_SIZE_MB,
        "has_modules": True,
    })


@trainer_required
@require_POST
def note_toggle_publish(request, pk):
    note, denied = _manageable_note_or_response(request, pk)
    if denied:
        return denied
    if note.is_published:
        note.unpublish()
        messages.success(request, f"“{note.title}” was unpublished. Students can no longer see it.")
    else:
        note.publish()
        messages.success(request, f"“{note.title}” is now published.")
    note.save(update_fields=["is_published", "published_at", "updated_at"])
    return redirect(_safe_next(request) or "core:notes_manage")


@trainer_required
@require_http_methods(["GET", "POST"])
def note_delete(request, pk):
    note, denied = _manageable_note_or_response(request, pk)
    if denied:
        return denied
    if request.method == "POST":
        title = note.title
        note.delete()  # the post_delete signal removes the file from disk
        messages.success(request, f"“{title}” was deleted.")
        return redirect("core:notes_manage")
    return render(request, "core/note_confirm_delete.html", {"note": note})


# --------------------------------------------------------------------------- #
# Reading and downloading
# --------------------------------------------------------------------------- #

def note_detail(request, pk):
    note = _visible_note_or_none(request, pk)
    if note is None:
        return _not_found(request)

    siblings = list(note.module.notes.published().order_by("-published_at", "title"))
    response = render(request, "core/note_detail.html", {
        "note": note,
        "can_manage": can_manage_note(request.user, note),
        "siblings": [n for n in siblings if n.pk != note.pk][:6],
    })
    response["Content-Security-Policy"] = READER_CSP
    return response


@xframe_options_sameorigin
def note_file(request, pk):
    """
    Deliver a note's file. PDFs open inline (or download with ?download=1);
    HTML is only ever offered as a download — never rendered as a live page
    from our own origin.
    """
    note = _visible_note_or_none(request, pk)
    if note is None:
        return _not_found(request)

    try:
        # Open via the storage so the handle carries an absolute path — that lets
        # Django send a Content-Length header.
        handle = note.file.storage.open(note.file.name, "rb")
    except (FileNotFoundError, ValueError):
        return render(request, "core/error_panel.html", {
            "title": "This file is unavailable",
            "detail": "The file for these notes is missing from the server. "
                      "Please tell the Trainer who added it, or an administrator.",
        }, status=404)

    as_attachment = note.is_html or request.GET.get("download") == "1"
    response = FileResponse(
        handle, as_attachment=as_attachment, filename=note.download_filename(),
        content_type="application/pdf" if note.is_pdf else "text/html; charset=utf-8")
    response["X-Content-Type-Options"] = "nosniff"
    if note.is_html:
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cache-Control"] = ("private, max-age=300" if note.is_published
                                 else "private, no-store")
    return response
