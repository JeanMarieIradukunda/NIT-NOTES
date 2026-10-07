"""
assessments.import_views
========================

Trainer uploads a Word/HTML assessment -> parsed into a draft -> the trainer
reviews it (and fills in any missing answers) -> questions are created in the
system's own format. The uploaded file is read in memory and never stored or
served back.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import importer
from .models import (FILL, MATCH, MCQ, OPEN, SECTION_LABELS, SECTION_ORDER, AnswerKey, ImportDraft,
                     Question)
from .permissions import trainer_required
from .staff_views import _exam_or_deny


def _draft_or_404(request, exam, draft_id):
    return get_object_or_404(ImportDraft, pk=draft_id, exam=exam)


@trainer_required
@require_http_methods(["GET", "POST"])
def import_upload(request, pk):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    ctx = {"exam": exam, "max_mb": importer.MAX_UPLOAD_BYTES // (1024 * 1024)}
    if request.method == "POST":
        f = request.FILES.get("file")
        if not f:
            ctx["error"] = "Choose a Word (.docx) or HTML (.html) file first."
            return render(request, "assessments/import_upload.html", ctx, status=400)
        if f.size > importer.MAX_UPLOAD_BYTES:
            ctx["error"] = f"That file is larger than {ctx['max_mb']} MB."
            return render(request, "assessments/import_upload.html", ctx, status=400)
        try:
            draft_data = importer.parse_upload(f.name, f.read())
        except importer.ImportProblem as exc:
            ctx["error"] = str(exc)
            return render(request, "assessments/import_upload.html", ctx, status=400)
        except Exception:
            ctx["error"] = "That file could not be read. Check it opens normally, or try saving it again as .docx."
            return render(request, "assessments/import_upload.html", ctx, status=400)
        ImportDraft.objects.filter(created_at__lt=timezone.now() - timedelta(days=2)).delete()
        ImportDraft.objects.filter(exam=exam, created_by=request.user).delete()
        draft = ImportDraft.objects.create(exam=exam, created_by=request.user,
                                           filename=f.name[:200], data=draft_data)
        return redirect("assessments:import_preview", pk=exam.pk, draft_id=draft.pk)
    return render(request, "assessments/import_upload.html", ctx)


def _suggest_marks(questions, header_marks, included):
    """{section: Decimal} from the marks written beside questions, else from the section heading."""
    out = {}
    for s in SECTION_ORDER:
        qs = [q for i, q in enumerate(questions) if i in included and q["section"] == s]
        if not qs:
            continue
        if all(q.get("marks") for q in qs):
            out[s] = sum((Decimal(str(q["marks"])) for q in qs), Decimal("0"))
        elif header_marks.get(s):
            out[s] = Decimal(str(header_marks[s]))
    return out


def _importable(q):
    s = q["section"]
    if s == MCQ:
        return q.get("correct") is not None
    if s == FILL:
        return bool(q.get("accepted"))
    if s == MATCH:
        return len(q.get("pairs") or {}) >= 2
    return True


@trainer_required
@require_http_methods(["GET", "POST"])
def import_preview(request, pk, draft_id):
    exam, denied = _exam_or_deny(request, pk)
    if denied:
        return denied
    draft = _draft_or_404(request, exam, draft_id)
    data = draft.data
    questions = data["questions"]
    has_attempts = exam.attempts.exists()

    if request.method == "POST":
        picked = []
        for i, q in enumerate(questions):
            q = dict(q)
            if request.POST.get(f"inc_{i}") != "1":
                continue
            if q["section"] == MCQ:
                c = request.POST.get(f"correct_{i}")
                if c is not None and c.isdigit() and int(c) < len(q["options"]):
                    q["correct"] = int(c)
            elif q["section"] == FILL:
                raw = request.POST.get(f"accepted_{i}", "")
                typed = [p.strip() for p in raw.replace("\n", "/").split("/") if p.strip()]
                if typed:
                    q["accepted"] = typed[:20]
            if not _importable(q):
                continue
            picked.append(q)
        if not picked:
            messages.error(request, "Nothing to import: tick at least one question that has its answer.")
            return redirect("assessments:import_preview", pk=exam.pk, draft_id=draft.pk)
        replace = request.POST.get("mode") == "replace" and not has_attempts
        # Marks written in the document replace the section marks only for a fresh or replaced
        # question set; mixing them with questions already weighted 1 would skew the sharing.
        apply = bool(request.POST.get("apply_marks")) and (replace or not exam.questions.exists())

        with transaction.atomic():
            if replace:
                exam.questions.all().delete()
            order = {s: (0 if replace else max([o for o in exam.questions.filter(section=s)
                                                .values_list("order", flat=True)] or [0])) for s in SECTION_ORDER}
            for q in picked:
                order[q["section"]] += 1
                weight = max(1, int(round(q["marks"]))) if q.get("marks") and apply else 1
                payload, key = {}, {}
                if q["section"] == MCQ:
                    payload, key = {"options": q["options"]}, {"correct": q["correct"]}
                elif q["section"] == FILL:
                    key = {"accepted": q["accepted"], "case_sensitive": False}
                elif q["section"] == OPEN:
                    key = {"guide": q.get("guide", "")}
                elif q["section"] == MATCH:
                    used = {int(j) for j in q["pairs"].values()}
                    left_idx = sorted(int(i) for i in q["pairs"])
                    left = [q["left"][i] for i in left_idx]
                    right = [q["right"][q["pairs"][str(i)]] for i in left_idx]
                    right += [t for j, t in enumerate(q["right"]) if j not in used]
                    payload, key = {"left": left, "right": right}, {"pairs": {str(n): n for n in range(len(left))}}
                obj = Question.objects.create(exam=exam, section=q["section"], order=order[q["section"]],
                                              text=q["text"], weight=weight, payload=payload)
                AnswerKey.objects.create(question=obj, data=key)
            changed = []
            if apply:
                for s, total in _suggest_marks(picked, data.get("header_marks", {}), set(range(len(picked)))).items():
                    setattr(exam, f"marks_{s}", total)
                    changed.append(s)
            if request.POST.get("use_intro") and data.get("intro") and not exam.instructions.strip():
                exam.instructions = data["intro"]
            exam.save()
            draft.delete()
        skipped = len([1 for i in range(len(questions)) if request.POST.get(f"inc_{i}") == "1"]) - len(picked)
        messages.success(request, f"Imported {len(picked)} question{'s' if len(picked) != 1 else ''}"
                                  + (f"; {skipped} skipped because their answer was missing." if skipped else ".")
                                  + (" Section marks were updated from the document." if changed else ""))
        return redirect("assessments:exam_detail", pk=exam.pk)

    rows = []
    for i, q in enumerate(questions):
        rows.append({"i": i, "q": q, "label": SECTION_LABELS[q["section"]], "ok": _importable(q),
                     "match_pairs": [(q["left"][int(a)], q["right"][b]) for a, b in
                                     sorted(q["pairs"].items(), key=lambda kv: int(kv[0]))] if q["section"] == MATCH else [],
                     "accepted_text": " / ".join(q.get("accepted", []))})
    suggestion = _suggest_marks(questions, data.get("header_marks", {}), set(range(len(questions))))
    return render(request, "assessments/import_preview.html", {
        "exam": exam, "draft": draft, "rows": rows, "warnings": data.get("warnings", []),
        "suggestion": [(SECTION_LABELS[s], v) for s, v in suggestion.items()],
        "n_ok": sum(1 for r in rows if r["ok"]), "has_attempts": has_attempts,
        "title": data.get("title", ""), "intro": data.get("intro", ""),
        "intro_usable": bool(data.get("intro")) and not exam.instructions.strip()})
