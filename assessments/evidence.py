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

from .marking import SECTION_ORDER, feedback_visible, review_questions
from .models import SECTION_LABELS

SECTION_CODES = {"mcq": "MCQ", "fill": "Fill", "match": "Match", "open": "Open"}

BRAND = {"institution": "Padri Vjeko Centre TSS", "department": "Department of Information Technology"}

# Tighter and tighter layouts. The first level that fits on one page wins.
# q / a / o / c = characters kept of the question, the answer, an open answer and the trainer's comment.
# "css" picks the type size and spacing (the .dN classes in _evidence_css.html); "level" is just the position.
DENSITIES = [
    {"level": 0, "css": 0, "cols": 1, "q": 110, "a": 220, "o": 500, "c": 320, "v": 8},
    {"level": 1, "css": 1, "cols": 1, "q": 90, "a": 140, "o": 260, "c": 200, "v": 6},
    {"level": 2, "css": 1, "cols": 2, "q": 80, "a": 110, "o": 220, "c": 170, "v": 6},
    {"level": 3, "css": 2, "cols": 2, "q": 60, "a": 80, "o": 150, "c": 120, "v": 5},
    {"level": 4, "css": 3, "cols": 2, "q": 42, "a": 52, "o": 90, "c": 70, "v": 4},
    {"level": 5, "css": 4, "cols": 2, "q": 28, "a": 34, "o": 54, "c": 44, "v": 3},
]

STATUS_LABELS = {"correct": "Correct", "partial": "Partly correct", "wrong": "Incorrect",
                 "marked": "Marked", "pending": "Awaiting marking", "blank": "No answer"}


def _start_level(n_questions):
    """Skip levels that obviously cannot fit so a big class does not render everything twice."""
    for limit, level in ((8, 0), (14, 1), (26, 2), (40, 3), (70, 4)):
        if n_questions <= limit:
            return level
    return len(DENSITIES) - 1


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


def _pct(score, top):
    """Whole-number percentage (0-100) for a progress bar."""
    try:
        return min(max(int(Decimal(str(score or 0)) * 100 / Decimal(str(top))), 0), 100)
    except Exception:
        return 0


def _tone(status, earned, top):
    """ok / part / bad / idle: the colour a question's marks are drawn in. Empty when marks are hidden."""
    if status == "correct":
        return "ok"
    if status == "partial":
        return "part"
    if status == "wrong":
        return "bad"
    if status == "marked" and earned is not None:
        return "ok" if earned >= top else "part" if earned > 0 else "bad"
    return "idle" if status else ""


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
    review = {r["n"]: r for r in review_questions(exam, attempt, comments=True)}
    show_score = trainer or exam.show_results
    show_marks = trainer or (exam.show_results and exam.show_answers)
    # Trainers always see the comments; a candidate sees them once marked (and results are released).
    show_comments = trainer or feedback_visible(exam, attempt)
    open_pending = exam.questions.filter(section="open").exists() and not attempt.marking_complete

    rows = []
    for n in sorted(text_rows):
        t, r = text_rows[n], review.get(n, {})
        status = r.get("status", "") if show_marks else ""
        earned = r.get("earned") if show_marks else None
        rows.append({"n": n, "section": t["section"], "code": SECTION_CODES.get(r.get("section"), t["section"]),
                     "question": t["question"], "answer": t["answer"], "answered": t["answered"], "max": t["max"],
                     "is_open": r.get("section") == "open", "earned": earned, "status": status,
                     "status_label": STATUS_LABELS.get(status, ""), "tone": _tone(status, earned, t["max"]),
                     "comment": (r.get("comment") or "") if show_comments else "",
                     "comment_auto": bool(r.get("comment")) and r.get("comment") == (attempt.open_marking or {}).get(f"fb:{r.get('qid')}")})

    sections = []
    for s_key in SECTION_ORDER:
        top = exam.section_marks(s_key)
        if top <= 0:
            continue
        score = attempt.section_scores.get(s_key) if s_key != "open" else attempt.open_total
        sections.append({"label": SECTION_LABELS[s_key], "max": top, "score": score,
                         "pct": _pct(score, top) if show_score else 0})

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
        "percent_bar": min(max(int(percent), 0), 100) if percent is not None else 0,
        "has_comments": any(r["comment"] for r in rows),
        "sections": sections, "rows": rows,
        "time_used": _duration(used),
        "opens": sum(d.opens for d in devices), "n_devices": len(devices),
        "browser": _browser(last.user_agent) if last else "", "ip": (last.ip if last else None) or "",
        "violations": [{"time": timezone.localtime(v.occurred_at), "label": v.label,
                        "marks": 0 if v.waived else v.marks_deducted, "waived": v.waived,
                        "detail": (v.detail + " · " if v.detail else "") + "deduction removed by trainer" if v.waived else v.detail}
                       for v in counted],
        "time_allowed": exam.duration_minutes + attempt.extra_minutes,
        "recovery": bool(attempt.recovery_log),
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
        c = clip(r.get("comment", ""), density["c"])
        clipped = clipped or len(re.sub(r"\s+", " ", r.get("comment", "")).strip()) > len(c)
        r["question"], r["answer"], r["comment"] = q, a, c
        rows.append(r)
    per_col = -(-len(rows) // density["cols"]) if rows else 0
    ev["columns"] = [rows[i:i + per_col] for i in range(0, len(rows), per_col)] if rows else []
    ev["clipped"] = clipped
    ev["violations_shown"] = ev["violations"][: density["v"]]
    ev["violations_more"] = max(len(ev["violations"]) - density["v"], 0)
    ev["sheet_no"], ev["sheet_total"] = sheet_no, sheet_total
    ev["doc_title"] = "Assessment evidence record" if ev["trainer"] else "Assessment answer sheet"
    ev["doc_sub"] = f"Ref. {ev['ref']}" + (f" · Sheet {sheet_no} of {sheet_total}" if sheet_no else "")
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


def describe_pdf_error(exc):
    """A short, plain-words reason a PDF could not be made, for the on-screen fallback page.

    Only shown to trainers. The commonest cause is not a missing package but WeasyPrint's native
    libraries (Pango/Cairo; GTK on Windows) failing to load, which Python reports as an OSError.
    """
    detail = " ".join(str(exc).split())[:240]
    if isinstance(exc, ModuleNotFoundError) and "weasyprint" in str(exc).lower():
        return "the WeasyPrint package is not installed in this environment (pip install weasyprint)"
    if isinstance(exc, OSError):
        return ("WeasyPrint is installed, but its system libraries (Pango and Cairo; on Windows, "
                "Pango from MSYS2) could not be loaded" + (f": {detail}" if detail else ""))
    return f"the PDF engine failed ({type(exc).__name__}" + (f": {detail}" if detail else "") + ")"


def bundle_html_context(exam, attempts, skipped=0):
    """On-screen fallback (print to PDF from the browser) when no PDF engine is installed."""
    generated = timezone.now()
    sheets = []
    for i, attempt in enumerate(attempts, start=1):
        ctx = build_context(attempt, trainer=True, generated=generated)
        sheets.append(apply_density(ctx, DENSITIES[min(_start_level(len(ctx["ev"]["rows"])) + 1, len(DENSITIES) - 1)], i, len(attempts)))
    return {"exam": exam, "brand": BRAND, "sheets": sheets, "d": sheets[0]["d"] if sheets else DENSITIES[1],
            "cover": _cover_context(exam, attempts, skipped, generated), "on_screen": True}
