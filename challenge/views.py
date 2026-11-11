"""
challenge.views
===============

IT Knowledge Challenge.

* The gallery, a card's own page and the card pictures are open to everyone: no account needed.
* A card that is not published is visible only to its author and to administrators.
* Adding cards is for trainers and administrators. A trainer files cards under their own modules and manages only
  their own cards; an administrator manages all of them.
"""

import logging
from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from accounts.roles import can_add_notes, is_admin, is_trainer, note_modules_for
from core.models import Module

from . import brand, images, render as card_render
from .forms import KnowledgeCardForm
from .models import KnowledgeCard
from .svg_clean import SVGError, clean_svg

logger = logging.getLogger(__name__)

# A card is a picture. Served this way even a hostile SVG can run nothing: no script, no network, no frames.
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


def can_manage_card(user, card):
    return is_admin(user) or (is_trainer(user) and card.uploaded_by_id is not None and card.uploaded_by_id == user.id)


def trainer_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not can_add_notes(request.user):
            return render(request, "core/error_panel.html", {
                "title": "Trainers only",
                "detail": "Adding cards is available to trainers and administrators. The gallery is open to everyone."},
                status=403)
        return view(request, *args, **kwargs)
    return wrapper


def _cards():
    return KnowledgeCard.objects.select_related("module", "module__trade", "uploaded_by")


def _visible_or_404(request, pk):
    card = get_object_or_404(_cards(), pk=pk)
    if not (card.is_published or (request.user.is_authenticated and can_manage_card(request.user, card))):
        raise Http404("No such card")
    return card


# --------------------------------------------------------------------------- #
# Public: gallery, card page, picture
# --------------------------------------------------------------------------- #

def gallery(request):
    cards = list(_cards().published().in_order()[:300])
    counts = {}
    for c in cards:
        counts.setdefault(c.module_id, [c.module, 0])[1] += 1
    topics = sorted(counts.values(), key=lambda t: (t[0].trade.order, t[0].order, t[0].code))
    try:
        active = int(request.GET.get("topic") or 0)
    except ValueError:
        active = 0
    if active not in counts:
        active = 0
    return render(request, "challenge/gallery.html", {
        "cards": cards, "topics": topics, "active": active, "NAME": brand.NAME, "TAGLINE": brand.TAGLINE,
        "can_add": request.user.is_authenticated and can_add_notes(request.user)})


def card_detail(request, pk):
    card = _visible_or_404(request, pk)
    published = list(_cards().published().in_order().values_list("pk", flat=True))
    prev_id = next_id = None
    if card.pk in published:
        i = published.index(card.pk)
        prev_id, next_id = published[i - 1] if i else None, published[i + 1] if i + 1 < len(published) else None
    return render(request, "challenge/detail.html", {
        "card": card, "prev_card": KnowledgeCard.objects.filter(pk=prev_id).first() if prev_id else None,
        "next_card": KnowledgeCard.objects.filter(pk=next_id).first() if next_id else None,
        "can_manage": request.user.is_authenticated and can_manage_card(request.user, card),
        "NAME": brand.NAME, "TAGLINE": brand.TAGLINE})


def card_picture(request, pk):
    """The card's picture: its SVG (generated or the trainer's own) or the PNG / JPG / WebP the trainer uploaded."""
    card = _visible_or_404(request, pk)
    if card.image:
        try:
            handle = card.image.storage.open(card.image.name, "rb")
        except (FileNotFoundError, ValueError):
            raise Http404("This picture is missing from the server.")
        except OSError:
            logger.exception("Could not read the picture for card %s", card.pk)
            return HttpResponse("The picture store did not respond. Please try again in a moment.", status=502,
                                content_type="text/plain; charset=utf-8")
        resp = FileResponse(handle, content_type=card.image_content_type,
                            as_attachment=bool(request.GET.get("download")), filename=card.download_name)
    else:
        resp = HttpResponse(card.svg(), content_type="image/svg+xml; charset=utf-8")
        if request.GET.get("download"):
            resp["Content-Disposition"] = f'attachment; filename="{card.download_name}"'
    resp["Content-Security-Policy"] = SVG_CSP
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "public, max-age=86400" if card.is_published else "private, no-store"
    return resp


card_svg = card_picture            # the older address keeps working


# --------------------------------------------------------------------------- #
# Trainer workspace
# --------------------------------------------------------------------------- #

@trainer_required
def manage(request):
    qs = _cards().in_order()
    if not is_admin(request.user):
        qs = qs.filter(uploaded_by=request.user)
    return render(request, "challenge/manage.html", {
        "cards": list(qs), "all_cards": is_admin(request.user), "NAME": brand.NAME})


def _save_form(request, form, card=None):
    new = card is None
    saved = form.save(commit=False)
    if new:
        saved.uploaded_by = request.user
    saved.save()
    for w in form.svg_warnings:
        messages.warning(request, w)
    messages.success(request, f"Card {'added' if new else 'saved'}." +
                     ("" if saved.is_published else " It is hidden until you tick “Show in the gallery”."))
    return saved


@trainer_required
@require_http_methods(["GET", "POST"])
def card_create(request):
    form = KnowledgeCardForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        _save_form(request, form)
        return redirect("challenge:manage")
    return render(request, "challenge/form.html", {"form": form, "card": None, "limits": card_render.LIMITS})


@trainer_required
@require_http_methods(["GET", "POST"])
def card_edit(request, pk):
    card = get_object_or_404(_cards(), pk=pk)
    if not can_manage_card(request.user, card):
        return render(request, "core/error_panel.html", {
            "title": "Not your card", "detail": "You can edit only the cards you added."}, status=403)
    form = KnowledgeCardForm(request.POST or None, request.FILES or None, instance=card, user=request.user)
    if request.method == "POST" and form.is_valid():
        _save_form(request, form, card)
        return redirect("challenge:manage")
    return render(request, "challenge/form.html", {"form": form, "card": card, "limits": card_render.LIMITS})


@require_POST
@trainer_required
def card_toggle(request, pk):
    card = get_object_or_404(_cards(), pk=pk)
    if not can_manage_card(request.user, card):
        return render(request, "core/error_panel.html", {"title": "Not your card", "detail": ""}, status=403)
    card.is_published = not card.is_published
    card.save(update_fields=["is_published", "updated_at"])
    messages.success(request, "Shown in the gallery." if card.is_published else "Hidden from the gallery.")
    return redirect("challenge:manage")


@trainer_required
@require_http_methods(["GET", "POST"])
def card_delete(request, pk):
    card = get_object_or_404(_cards(), pk=pk)
    if not can_manage_card(request.user, card):
        return render(request, "core/error_panel.html", {"title": "Not your card", "detail": ""}, status=403)
    if request.method == "POST":
        card.delete()
        messages.success(request, "Card deleted.")
        return redirect("challenge:manage")
    return render(request, "challenge/confirm_delete.html", {"card": card})


@trainer_required
def card_bulk(request):
    """Upload many finished memes at once (the page sends them one at a time, see card_upload)."""
    modules = note_modules_for(request.user).select_related("trade").order_by("trade__order", "order", "code")
    return render(request, "challenge/bulk.html", {
        "modules": modules, "max_mb": images.size_text(images.MAX_IMAGE_BYTES), "NAME": brand.NAME})


def _upload_error(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


@require_POST
def card_upload(request):
    """
    One finished meme (PNG, JPG, WebP or SVG) becomes one card under one of the trainer's modules. Called once per
    file by the upload page, because Vercel rejects a request body over about 4.5 MB.
    """
    if not request.user.is_authenticated:
        return _upload_error("Your session ended. Please sign in again.", 401)
    if not can_add_notes(request.user):
        return _upload_error("Adding cards is for trainers and administrators.", 403)
    module = note_modules_for(request.user).filter(pk=request.POST.get("module") or 0).first()
    if module is None:
        return _upload_error("Choose one of your own modules.")
    f = request.FILES.get("file")
    if f is None:
        return _upload_error("No file was received.")
    if f.size > images.MAX_IMAGE_BYTES:
        return _upload_error(f"{f.name} is {images.size_text(f.size)}. Please keep each picture under "
                             f"{images.size_text(images.MAX_IMAGE_BYTES)} (a free tool such as tinypng.com can shrink it).")
    data = f.read()
    title_limit = card_render.LIMITS["title"]
    title = (request.POST.get("title") or "").strip()[:title_limit] or images.title_from_filename(f.name, title_limit)
    card = KnowledgeCard(module=module, title=title, uploaded_by=request.user,
                         is_published=request.POST.get("publish") == "1")
    ext = images.detect_image(data)
    if ext:
        card.image.save(f"card{ext}", ContentFile(data), save=False)
    else:
        try:
            card.custom_svg, _warnings = clean_svg(data)
        except SVGError:
            return _upload_error(f"{f.name} is not a PNG, JPG, WebP or SVG picture.")
    card.save()
    return JsonResponse({"ok": True, "id": card.pk, "title": card.title, "kind": card.picture_kind,
                         "url": card.get_absolute_url(), "edit": reverse("challenge:card_edit", args=[card.pk])})


@require_POST
@trainer_required
def card_preview(request):
    """The card as it would look right now, from the form's current values. Nothing is saved."""
    data = {k: (request.POST.get(k) or "").strip() for k in card_render.LIMITS}
    for k, limit in card_render.LIMITS.items():
        data[k] = data[k][:limit] or {"title": "Your title", "bubble": "Your funny line", "question": "Your question",
                                      "answer": "Your answer", "explanation": "Your explanation"}[k]
    module = Module.objects.filter(pk=request.POST.get("module") or 0).first()
    from core.context_processors import brand as site
    svg = card_render.render_card({
        **data, "character": request.POST.get("character"), "mood": request.POST.get("mood"),
        "module_code": module.code if module else "MODULE", "module_name": module.name if module else "",
        "number": int(request.POST.get("number") or 0) or 1, "palette": module.pk if module else 0},
        site(None)["BRAND"]["institution"])
    resp = HttpResponse(svg, content_type="image/svg+xml; charset=utf-8")
    resp["Content-Security-Policy"] = SVG_CSP
    resp["Cache-Control"] = "no-store"
    return resp
