"""
assessments.evidence
====================

The one-page "Assessment Evidence Record" for a submitted attempt, and the
class bundle a trainer downloads (a register page, then one candidate per
independent page).

* build_context()      collects every detail printed on a sheet.
* render_sheet()       renders one sheet and, if it spills onto a second page,
                       re-renders it with tighter type and clipping until it fits
                       on ONE A4 page (see DENSITIES).
* bundle_pdf()         register + one sheet per candidate, each starting on its own page.
* sheet_html_context() the same data for the on-screen fallback when no PDF engine exists.

The answer key is never printed. Per-question marks are printed for trainers, and
for candidates only when the trainer has released both results and answers.
"""

import base64
import hashlib
import hmac
import io
import json
import re
from decimal import Decimal
from functools import lru_cache

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from .marking import SECTION_ORDER, review_questions
from .models import SECTION_LABELS

SECTION_CODES = {"mcq": "MCQ", "fill": "Fill", "match": "Match", "open": "Open"}

BRAND = {"institution": "Padri Vjeko Centre TSS", "department": "Department of Information Technology"}

# Tighter and tighter layouts. The first level that fits on one page wins.
DENSITIES = [
    {"level": 0, "cols": 1, "q": 110, "a": 220, "o": 500, "v": 8},
    {"level": 1, "cols": 1, "q": 90, "a": 140, "o": 260, "v": 6},
    {"level": 2, "cols": 2, "q": 60, "a": 80, "o": 150, "v": 5},
    {"level": 3, "cols": 2, "q": 42, "a": 52, "o": 90, "v": 4},
    {"level": 4, "cols": 2, "q": 28, "a": 34, "o": 54, "v": 3},
]


def _start_level(n_questions):
    """Skip levels that obviously cannot fit so a big class does not render everything twice."""
    for limit, level in ((12, 0), (22, 1), (40, 2), (70, 3)):
        if n_questions <= limit:
            return level
    return 4


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def logo_data_uri():
    """The school logo, shrunk once and embedded, so PDF rendering never makes a network request."""
    try:
        from PIL import Image
        path = settings.BASE_DIR / "static" / "img" / "school.png"
        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((220, 220))
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def clip(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: max(limit - 1, 1)].rstrip() + "…"


def _browser(ua):
    ua = ua or ""
    if not ua:
        return ""
    browser = next((name for token, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                             ("Chrome/", "Chrome"), ("Safari/", "Safari")) if token in ua), "Browser")
    system = next((name for token, name in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"),
                                            ("iPad", "iPadOS"), ("Mac OS", "macOS"), ("Linux", "Linux"))
                   if token in ua), "")
    return f"{browser} on {system}" if system else browser


def _duration(delta):
    if delta is None:
        return ""
    seconds = max(int(delta.total_seconds()), 0)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes} min {secs:02d} s"


def integrity_code(attempt):
    """
    A short keyed fingerprint of the stored record (answers, marks, times). It is regenerated
    from the database, so if anything on the record is later changed the code printed on an
    older sheet no longer matches the code on a freshly generated one.
    """
    payload = json.dumps([
        attempt.exam_id, attempt.pk, attempt.reg_no, attempt.answers, attempt.open_marks,
        attempt.mark_overrides, str(attempt.final_score), str(attempt.penalty_total),
        attempt.submitted_at.isoformat() if attempt.submitted_at else "",
    ], sort_keys=True, default=str)
    digest = hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest().upper()[:16]
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_") or "candidate"


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #

def build_context(attempt, *, trainer, generated=None):
    """Everything one evidence sheet prints, as plain data (no clipping yet)."""
    from .views import _sheet_rows                      # lazy: views imports this module

    exam = attempt.exam
    generated = generated or timezone.now()
    text_rows = {r["n"]: r for r in _sheet_rows(attempt)}
    review = {r["n"]: r for r in review_questions(exam, attempt)}
    show_score = trainer or exam.show_results
    show_marks = trainer or (exam.show_results and exam.show_answers)
    open_pending = exam.questions.filter(section="open").exists() and not attempt.marking_complete

    rows = []
    for n in sorted(text_rows):
        t, r = text_rows[n], review.get(n, {})
        rows.append({"n": n, "section": t["section"], "code": SECTION_CODES.get(r.get("section"), t["section"]), "question": t["question"], "answer": t["answer"],
                     "answered": t["answered"], "max": t["max"], "is_open": r.get("section") == "open",
                     "earned": r.get("earned") if show_marks else None,
                     "status": r.get("status", "") if show_marks else ""})

    sections = [{"label": SECTION_LABELS[s], "max": exam.section_marks(s),
                 "score": attempt.section_scores.get(s) if s != "open" else attempt.open_total}
                for s in SECTION_ORDER if exam.section_marks(s) > 0]

    total = exam.total_marks
    percent = None
    if show_score and attempt.final_score is not None and total:
        percent = (Decimal(attempt.final_score) * 100 / total).quantize(Decimal("0.1"))

    devices = list(attempt.devices.all())
    last = max(devices, key=lambda d: d.last_opened_at) if devices else None
    counted = [v for v in attempt.violations.all() if v.counted]
    used = (attempt.submitted_at - attempt.started_at) if (attempt.submitted_at and attempt.started_at) else None

    ev = {
        "ref": f"{exam.public_id}-{attempt.reg_no}",
        "integrity": integrity_code(attempt),
        "show_score": show_score, "show_marks": show_marks, "trainer": trainer,
        "open_pending": open_pending, "percent": percent, "total": total,
        "sections": sections, "rows": rows,
        "time_used": _duration(used),
        "opens": sum(d.opens for d in devices), "n_devices": len(devices),
        "browser": _browser(last.user_agent) if last else "", "ip": (last.ip if last else None) or "",
        "violations": [{"time": timezone.localtime(v.occurred_at), "label": v.label, "marks": v.marks_deducted,
                        "detail": v.detail} for v in counted],
        "uncounted": attempt.violations.filter(counted=False).count(),
        "earlier_chances": len(attempt.previous_attempts or []),
        "comment": attempt.trainer_comment if trainer else "",
        "generated": timezone.localtime(generated),
    }
    return {"attempt": attempt, "exam": exam, "ev": ev, "brand": BRAND, "logo": logo_data_uri()}


def apply_density(ctx, density, sheet_no=None, sheet_total=None):
    """A copy of the context with rows clipped to the density and split into columns."""
    ev = dict(ctx["ev"])
    clipped = False
    rows = []
    for r in ev["rows"]:
        r = dict(r)
        q = clip(r["question"], density["q"])
        a = clip(r["answer"], density["o"] if r["is_open"] else density["a"])
        clipped = clipped or len(re.sub(r"\s+", " ", r["question"]).strip()) > len(q) \
            or len(re.sub(r"\s+", " ", r["answer"]).strip()) > len(a)
        r["question"], r["answer"] = q, a
        rows.append(r)
    per_col = -(-len(rows) // density["cols"]) if rows else 0
    ev["columns"] = [rows[i:i + per_col] for i in range(0, len(rows), per_col)] if rows else []
    ev["clipped"] = clipped
    ev["violations_shown"] = ev["violations"][: density["v"]]
    ev["violations_more"] = max(len(ev["violations"]) - density["v"], 0)
    ev["sheet_no"], ev["sheet_total"] = sheet_no, sheet_total
    return {**ctx, "ev": ev, "d": density}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_sheet(ctx, *, sheet_no=None, sheet_total=None):
    """Return a WeasyPrint Document for one candidate, on one page whenever that is possible."""
    from weasyprint import HTML
    start = _start_level(len(ctx["ev"]["rows"]))
    doc = None
    for density in DENSITIES[start:]:
        html = render_to_string("assessments/answer_sheet.html",
                                apply_density(ctx, density, sheet_no, sheet_total))
        doc = HTML(string=html).render()
        if len(doc.pages) <= 1:
            break
    return doc


def sheet_pdf(attempt, *, trainer):
    return render_sheet(build_context(attempt, trainer=trainer)).write_pdf()


def register_rows(attempts, generated):
    rows = []
    for i, a in enumerate(attempts, start=1):
        rows.append({"n": i, "name": a.candidate_name, "reg": a.reg_no_display or a.reg_no,
                     "submitted": timezone.localtime(a.submitted_at) if a.submitted_at else None,
                     "reason": a.get_submit_reason_display(), "violations": a.violation_count,
                     "penalty": a.penalty_total, "score": a.final_score,
                     "pending": not a.marking_complete})
    return rows


def _cover_context(exam, attempts, skipped, generated):
    scores = [a.final_score for a in attempts if a.final_score is not None]
    return {"exam": exam, "brand": BRAND, "logo": logo_data_uri(), "rows": register_rows(attempts, generated),
            "skipped": skipped, "generated": timezone.localtime(generated), "total": exam.total_marks,
            "average": (sum(scores, Decimal("0")) / len(scores)).quantize(Decimal("0.01")) if scores else None,
            "highest": max(scores) if scores else None, "lowest": min(scores) if scores else None}


def bundle_pdf(exam, attempts, skipped=0):
    return bundle_document(exam, attempts, skipped).write_pdf()


def bundle_document(exam, attempts, skipped=0):
    """Register page(s), then every candidate on an independent page (a WeasyPrint Document)."""
    from weasyprint import HTML
    generated = timezone.now()
    cover = HTML(string=render_to_string("assessments/evidence_cover.html",
                                         _cover_context(exam, attempts, skipped, generated))).render()
    docs = [cover]
    for i, attempt in enumerate(attempts, start=1):
        ctx = build_context(attempt, trainer=True, generated=generated)
        docs.append(render_sheet(ctx, sheet_no=i, sheet_total=len(attempts)))
    pages = [p for d in docs for p in d.pages]
    return cover.copy(pages)


def bundle_html_context(exam, attempts, skipped=0):
    """On-screen fallback (print to PDF from the browser) when no PDF engine is installed."""
    generated = timezone.now()
    sheets = []
    for i, attempt in enumerate(attempts, start=1):
        ctx = build_context(attempt, trainer=True, generated=generated)
        sheets.append(apply_density(ctx, DENSITIES[min(_start_level(len(ctx["ev"]["rows"])) + 1, 4)], i, len(attempts)))
    return {"exam": exam, "brand": BRAND, "sheets": sheets, "d": sheets[0]["d"] if sheets else DENSITIES[1],
            "cover": _cover_context(exam, attempts, skipped, generated), "on_screen": True}
