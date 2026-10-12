"""Trainer screens for an exam's class list."""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from . import roster as roster_lib
from .class_views import save_members, visible_classes
from .models import Attempt, ClassGroup, ClassMember, RosterEntry
from .permissions import trainer_required
from .staff_views import _exam_or_deny


@trainer_required
@require_http_methods(["GET", "POST"])
def roster_page(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    if request.method == "POST":
        text = (request.POST.get("paste") or "").strip()
        f = request.FILES.get("file")
        if f:
            if f.size > roster_lib.MAX_BYTES:
                messages.error(request, "That file is too large for a class list.")
                return redirect("assessments:roster", pk=exam.pk)
            text = roster_lib.decode(f.read())
        if not text.strip():
            messages.error(request, "Paste the class list or choose a CSV/TXT file first.")
            return redirect("assessments:roster", pk=exam.pk)
        rows, problems = roster_lib.parse_roster(text)
        if not rows:
            messages.error(request, "No candidates were found. Use one per line: candidate number, full name.")
            for p in problems[:5]:
                messages.warning(request, p)
            return redirect("assessments:roster", pk=exam.pk)
        added = updated = 0
        with transaction.atomic():
            if request.POST.get("mode") == "replace":
                exam.roster.all().delete()
            existing = {e.reg_no: e for e in exam.roster.all()}
            for r in rows:
                e = existing.get(r["reg_no"])
                if e is None:
                    RosterEntry.objects.create(exam=exam, **r)
                    added += 1
                elif e.name != r["name"] or e.reg_no_display != r["reg_no_display"]:
                    e.name, e.reg_no_display = r["name"] or e.name, r["reg_no_display"]
                    e.save(update_fields=["name", "reg_no_display"])
                    updated += 1
        messages.success(request, f"Class list saved: {added} added, {updated} updated"
                                  + (f", {len(problems)} line(s) skipped." if problems else "."))
        save_as = (request.POST.get("save_as") or "").strip()[:120]
        if save_as:
            group, made = ClassGroup.objects.get_or_create(name=save_as, created_by=request.user)
            n_added, n_updated = save_members(group, rows)
            messages.success(request, f"Also saved as class “{group.name}” ({n_added} added, {n_updated} updated) "
                                      "so you can reuse it on other assessments.")
        for p in problems[:8]:
            messages.warning(request, p)
        return redirect("assessments:roster", pk=exam.pk)

    attempts = {a.reg_no: a for a in exam.attempts.all()}
    entries = list(exam.roster.all())
    rows = []
    for e in entries:
        a = attempts.get(e.reg_no)
        rows.append({"e": e, "status": ("submitted" if a and a.is_submitted else "in_progress" if a and a.end_at
                                        else "opened" if a else "not_started")})
    listed = {e.reg_no for e in entries}
    unlisted = [a for a in attempts.values() if a.reg_no not in listed] if entries else []
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("not_started", "opened", "in_progress", "submitted")}
    classes = list(visible_classes(request.user).annotate(n_members=Count("members")))

    # "Pick students from a class": the chosen class (?from=<id>, or the only class there is) with each
    # of its students marked if they are already on this assessment's list.
    chosen = None
    wanted = request.GET.get("from", "")
    if wanted.isdigit():
        chosen = next((c for c in classes if c.pk == int(wanted)), None)
    elif len(classes) == 1:
        chosen = classes[0]
    picker = []
    if chosen:
        picker = [{"m": m, "on_list": m.reg_no in listed} for m in chosen.members.all()]
    return render(request, "assessments/roster.html", {
        "exam": exam, "rows": rows, "counts": counts, "unlisted": unlisted, "total": len(entries),
        "classes": classes, "picker_class": chosen, "picker": picker,
        "picker_new": sum(1 for r in picker if not r["on_list"])})


@require_POST
@trainer_required
def roster_assign(request, pk):
    """Publish this assessment to one or more saved classes: their candidates are copied onto its class list."""
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    ids = [int(i) for i in request.POST.getlist("classes") if i.isdigit()]
    groups = list(visible_classes(request.user).filter(pk__in=ids).prefetch_related("members"))
    if not groups:
        messages.error(request, "Choose at least one class.")
        return redirect("assessments:roster", pk=exam.pk)
    members = [m for g in groups for m in g.members.all()]
    if not members:
        messages.error(request, "The selected class has no candidates yet. Open it under Classes and add some.")
        return redirect("assessments:roster", pk=exam.pk)
    added = updated = 0
    with transaction.atomic():
        if request.POST.get("mode") == "replace":
            exam.roster.all().delete()
        existing = {e.reg_no: e for e in exam.roster.all()}
        for m in members:
            e = existing.get(m.reg_no)
            if e is None:
                existing[m.reg_no] = RosterEntry.objects.create(
                    exam=exam, reg_no=m.reg_no, reg_no_display=m.reg_no_display, name=m.name)
                added += 1
            elif (m.name and e.name != m.name) or e.reg_no_display != m.reg_no_display:
                e.name, e.reg_no_display = m.name or e.name, m.reg_no_display
                e.save(update_fields=["name", "reg_no_display"])
                updated += 1
    names = ", ".join(f"“{g.name}”" for g in groups)
    messages.success(request, f"{names} applied: {added} candidate{'s' if added != 1 else ''} added"
                              + (f", {updated} updated." if updated else "."))
    if not exam.is_open:
        messages.info(request, "The assessment is still closed. Open it from the assessment page to publish it to the class.")
    return redirect("assessments:roster", pk=exam.pk)


@require_POST
@trainer_required
def roster_add_members(request, pk):
    """Add the ticked students of a saved class to this assessment's class list.

    Unlike roster_assign (which copies whole classes), this adds only the chosen students. Anyone
    already on the list is left alone. Only students from classes the trainer can see are accepted.
    """
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    back = reverse("assessments:roster", args=[exam.pk])
    if (request.POST.get("from") or "").isdigit():
        back += f"?from={int(request.POST['from'])}"
    ids = [int(i) for i in request.POST.getlist("members") if i.isdigit()]
    chosen = list(ClassMember.objects.filter(pk__in=ids, group__in=visible_classes(request.user))
                  .select_related("group"))
    if not chosen:
        messages.error(request, "Tick at least one student to add.")
        return redirect(back)

    had_list = exam.roster.exists()
    added = already = 0
    with transaction.atomic():
        existing = {e.reg_no for e in exam.roster.all()}
        for m in chosen:
            if m.reg_no in existing:
                already += 1
                continue
            RosterEntry.objects.create(exam=exam, reg_no=m.reg_no, reg_no_display=m.reg_no_display, name=m.name)
            existing.add(m.reg_no)
            added += 1
    parts = [f"Added {added} student{'s' if added != 1 else ''} to the class list"]
    if already:
        parts.append(f"{already} {'was' if already == 1 else 'were'} already on it")
    messages.success(request, ", ".join(parts) + ".")
    if added and not had_list:
        messages.info(request, "Entry is now limited to the students on this list. Anyone else with the link "
                               "and password will be turned away.")
    if added and not exam.is_open:
        messages.info(request, "The assessment is still closed. Open it from the assessment page when you are ready.")
    return redirect(back)


@require_POST
@trainer_required
def roster_delete(request, pk, rid):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    get_object_or_404(RosterEntry, pk=rid, exam=exam).delete()
    messages.success(request, "Removed from the class list.")
    return redirect("assessments:roster", pk=exam.pk)


@require_POST
@trainer_required
def roster_clear(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    exam.roster.all().delete()
    messages.success(request, "Class list cleared. Anyone with the link and password can enter again.")
    return redirect("assessments:roster", pk=exam.pk)
