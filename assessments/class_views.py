"""
Saved classes
=============

A trainer uploads a class list once (pasted, or a CSV/TXT file) and gives it a name. Any assessment can
then use it: on the assessment's class-list page the trainer picks one or more saved classes and the
candidates are copied into that assessment. Administrators see every class; a Trainer sees their own.
"""

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.roles import is_admin

from . import roster as roster_lib
from .models import ClassGroup, ClassMember
from .permissions import deny, trainer_required


def visible_classes(user):
    qs = ClassGroup.objects.all()
    return qs if is_admin(user) else qs.filter(created_by=user)


def _class_or_deny(request, cid):
    group = get_object_or_404(ClassGroup, pk=cid)
    if not is_admin(request.user) and group.created_by_id != request.user.id:
        return None, deny(request, "You can only manage your own classes", "This class was saved by another Trainer.")
    return group, None


def read_roster_input(request):
    """(rows, problems, error) from the pasted box or uploaded CSV/TXT in a POST."""
    text = (request.POST.get("paste") or "").strip()
    f = request.FILES.get("file")
    if f:
        if f.size > roster_lib.MAX_BYTES:
            return [], [], "That file is too large for a class list."
        text = roster_lib.decode(f.read())
    if not text.strip():
        return [], [], "Paste the class list or choose a CSV/TXT file first."
    rows, problems = roster_lib.parse_roster(text)
    if not rows:
        return [], problems, "No candidates were found. Use one per line: candidate number, full name."
    return rows, problems, ""


def save_members(group, rows, replace=False):
    """Add (or replace) members of a saved class. Returns (added, updated)."""
    added = updated = 0
    with transaction.atomic():
        if replace:
            group.members.all().delete()
        existing = {m.reg_no: m for m in group.members.all()}
        for r in rows:
            m = existing.get(r["reg_no"])
            if m is None:
                ClassMember.objects.create(group=group, **r)
                added += 1
            elif m.name != r["name"] or m.reg_no_display != r["reg_no_display"]:
                m.name, m.reg_no_display = r["name"] or m.name, r["reg_no_display"]
                m.save(update_fields=["name", "reg_no_display"])
                updated += 1
    return added, updated


@trainer_required
@require_http_methods(["GET", "POST"])
def class_list(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:120]
        if not name:
            messages.error(request, "Give the class a name, for example “Year 2 Networking”.")
            return redirect("assessments:class_list")
        rows, problems, error = read_roster_input(request)
        if error:
            messages.error(request, error)
            for p in problems[:5]:
                messages.warning(request, p)
            return redirect("assessments:class_list")
        try:
            with transaction.atomic():
                group = ClassGroup.objects.create(name=name, created_by=request.user)
        except IntegrityError:
            messages.error(request, f"You already have a class called “{name}”. Open it to add more candidates.")
            return redirect("assessments:class_list")
        added, _ = save_members(group, rows)
        messages.success(request, f"Class “{group.name}” saved with {added} candidate{'s' if added != 1 else ''}"
                                  + (f", {len(problems)} line(s) skipped." if problems else "."))
        for p in problems[:8]:
            messages.warning(request, p)
        return redirect("assessments:class_detail", cid=group.pk)
    classes = visible_classes(request.user).select_related("created_by").annotate(n_members=Count("members"))
    return render(request, "assessments/classes.html", {"classes": classes, "show_owner": is_admin(request.user)})


@trainer_required
@require_http_methods(["GET", "POST"])
def class_detail(request, cid):
    group, denied = _class_or_deny(request, cid)
    if denied:
        return denied
    if request.method == "POST":
        rows, problems, error = read_roster_input(request)
        if error:
            messages.error(request, error)
            for p in problems[:5]:
                messages.warning(request, p)
            return redirect("assessments:class_detail", cid=group.pk)
        added, updated = save_members(group, rows, replace=request.POST.get("mode") == "replace")
        messages.success(request, f"Class saved: {added} added, {updated} updated"
                                  + (f", {len(problems)} line(s) skipped." if problems else "."))
        for p in problems[:8]:
            messages.warning(request, p)
        return redirect("assessments:class_detail", cid=group.pk)
    members = list(group.members.all())
    return render(request, "assessments/class_detail.html", {"group": group, "members": members, "total": len(members)})


@require_POST
@trainer_required
def class_member_delete(request, cid, mid):
    group, denied = _class_or_deny(request, cid)
    if denied:
        return denied
    get_object_or_404(ClassMember, pk=mid, group=group).delete()
    messages.success(request, "Removed from the class.")
    return redirect("assessments:class_detail", cid=group.pk)


@require_POST
@trainer_required
def class_delete(request, cid):
    group, denied = _class_or_deny(request, cid)
    if denied:
        return denied
    name = group.name
    group.delete()
    messages.success(request, f"Class “{name}” deleted. Assessments that already used it keep their own copy of the list.")
    return redirect("assessments:class_list")
