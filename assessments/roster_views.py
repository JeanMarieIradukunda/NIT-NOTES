"""Trainer screens for an exam's class list."""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import roster as roster_lib
from .models import Attempt, RosterEntry
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
    return render(request, "assessments/roster.html", {
        "exam": exam, "rows": rows, "counts": counts, "unlisted": unlisted, "total": len(entries)})


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
