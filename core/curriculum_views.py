"""
core.curriculum_views
=======================

Administrator-only management of the curriculum structure: Trades (levels)
and Modules. This is the in-app replacement for what used to live at
/admin/core/trade/ and /admin/core/module/ — the Django admin site itself has
been removed (see nit_platform/urls.py); this is the only way to manage
Trades and Modules now.

Everything else that used to be reachable from the Django admin index
(Lessons, Module notes, Activities, Resources, Users) already has its own
in-app workspace — /notes/, /activities/, /accounts/trainers/, and the
per-module "Upload lesson notes" flow — so nothing else needed to move.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from accounts.roles import is_admin
from .forms import ModuleForm, TradeForm
from .models import Module, Trade


def admin_required(view):
    """Signed-in Administrators only. Everyone else gets a clear 403."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_admin(request.user):
            return render(request, "core/error_panel.html", {
                "title": "Administrators only",
                "detail": "Managing levels and modules is available to Administrators only.",
            }, status=403)
        return view(request, *args, **kwargs)
    return wrapper


def _trades_with_counts():
    return (Trade.objects.annotate(module_count=Count("modules", distinct=True))
            .order_by("order", "name"))


def _modules_with_counts():
    return (Module.objects.select_related("trade")
            .annotate(unit_count=Count("units", distinct=True),
                      lesson_count_db=Count("units__lessons", distinct=True),
                      note_count_db=Count("notes", distinct=True),
                      activity_count_db=Count("activities", distinct=True))
            .order_by("trade__order", "order", "code"))


@admin_required
def curriculum(request):
    """The Trade/Module structure, grouped by level — replaces the Django
    admin Trade list (with its inline Module rows) and Module list."""
    return render(request, "core/curriculum.html", {
        "trades": _trades_with_counts(),
        "modules": _modules_with_counts(),
    })


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #

@admin_required
def trade_create(request):
    if request.method == "POST":
        form = TradeForm(request.POST)
        if form.is_valid():
            trade = form.save()
            messages.success(request, f"“{trade.name}” was added.")
            return redirect("core:curriculum")
    else:
        form = TradeForm()
    return render(request, "core/trade_form.html", {"form": form, "trade": None})


@admin_required
def trade_edit(request, pk):
    trade = get_object_or_404(Trade, pk=pk)
    if request.method == "POST":
        form = TradeForm(request.POST, instance=trade)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{trade.name}” was updated.")
            return redirect("core:curriculum")
    else:
        form = TradeForm(instance=trade)
    return render(request, "core/trade_form.html", {"form": form, "trade": trade})


@admin_required
def trade_delete(request, pk):
    trade = get_object_or_404(Trade.objects.annotate(module_count=Count("modules")), pk=pk)
    if trade.module_count:
        messages.error(
            request,
            f"“{trade.name}” still has {trade.module_count} module"
            f"{'s' if trade.module_count != 1 else ''} in it. Move or delete "
            "those first, then delete the level.")
        return redirect("core:curriculum")
    if request.method == "POST":
        name = trade.name
        trade.delete()
        messages.success(request, f"“{name}” was deleted.")
        return redirect("core:curriculum")
    return render(request, "core/trade_confirm_delete.html", {"trade": trade})


# --------------------------------------------------------------------------- #
# Modules
# --------------------------------------------------------------------------- #

@admin_required
def module_create(request):
    initial = {}
    trade_id = request.GET.get("trade")
    if trade_id and trade_id.isdigit():
        initial["trade"] = trade_id
    if request.method == "POST":
        form = ModuleForm(request.POST)
        if form.is_valid():
            module = form.save()
            messages.success(request, f"“{module.code}” was added.")
            return redirect("core:curriculum")
    else:
        form = ModuleForm(initial=initial)
    return render(request, "core/module_form.html", {"form": form, "module": None})


@admin_required
def module_edit(request, pk):
    module = get_object_or_404(Module, pk=pk)
    if request.method == "POST":
        form = ModuleForm(request.POST, instance=module)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{module.code}” was updated.")
            return redirect("core:curriculum")
    else:
        form = ModuleForm(instance=module)
    return render(request, "core/module_form.html", {"form": form, "module": module})


@admin_required
def module_delete(request, pk):
    module = get_object_or_404(
        Module.objects.select_related("trade").annotate(
            unit_count=Count("units", distinct=True),
            lesson_count_db=Count("units__lessons", distinct=True),
            note_count_db=Count("notes", distinct=True),
            activity_count_db=Count("activities", distinct=True)),
        pk=pk)
    trainer_count = module.trainers.count()
    if request.method == "POST":
        code = module.code
        module.delete()   # cascades to Units, Lessons, Module notes, Activities,
                          # Resources — each model's own post_delete signal
                          # still removes its file from storage as it goes.
        messages.success(request, f"“{code}” and everything filed under it was deleted.")
        return redirect("core:curriculum")
    return render(request, "core/module_confirm_delete.html", {
        "module": module, "trainer_count": trainer_count,
    })
