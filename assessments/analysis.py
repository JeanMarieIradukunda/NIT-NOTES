"""
assessments.analysis
====================

Item analysis for trainers: how each question performed across SUBMITTED
attempts, and which questions may need a second look. Trainer-only; reads the
answer key.
"""

import statistics
from collections import Counter
from decimal import Decimal

from .marking import (OVERRIDABLE_SECTIONS, _canonical, clean_answer, correct_set, is_multi, mark_question, ordered_questions,
                      question_max_marks, right_id)
from .models import FILL, MATCH, MCQ, OBJECTIVE_SECTIONS, OPEN, SECTION_LABELS, Attempt

MIN_FOR_FLAGS = 5            # fewer submitted attempts than this: figures are shown but not judged
MIN_FOR_DISCRIMINATION = 10


def _pct(n, d):
    return round(100 * n / d) if d else None


def analyse(exam):
    attempts = list(exam.attempts.filter(status=Attempt.SUBMITTED))
    n = len(attempts)
    qs = _canonical(list(exam.questions.select_related("key")))
    maxima = question_max_marks(exam, qs)

    gross = {a.pk: Decimal(a.objective_score) + a.open_total for a in attempts}
    ranked = sorted(attempts, key=lambda a: gross[a.pk])
    k = max(1, round(n * 0.27)) if n >= MIN_FOR_DISCRIMINATION else 0
    lower = {a.pk for a in ranked[:k]}
    upper = {a.pk for a in ranked[-k:]} if k else set()

    items = []
    for num, q in enumerate(qs, 1):
        key = q.key.data if hasattr(q, "key") else {}
        mx = maxima[q.id]
        it = {"n": num, "section": q.section, "label": SECTION_LABELS[q.section], "text": q.text, "max": mx,
              "flags": [], "answered": 0, "full": 0, "avg": None, "pct_correct": None, "disc": None,
              "options": [], "wrong_answers": [], "pairs": [], "marked": 0, "pending": 0}
        earned_all, frac_up, frac_low, picks, wrongs = [], [], [], Counter(), Counter()
        pair_ok = Counter()
        for a in attempts:
            raw = (a.answers or {}).get(str(q.id))
            ans = clean_answer(q, raw)
            if ans is not None:
                it["answered"] += 1
            if q.section in OBJECTIVE_SECTIONS:
                e = mark_question(q, key, raw, mx, exam.multi_scoring)
                ov = (a.mark_overrides or {}).get(str(q.id))
                if ov is not None and q.section in OVERRIDABLE_SECTIONS:
                    e = min(Decimal(str(ov)), mx)               # the trainer's adjusted mark counts
                earned_all.append(e)
                if mx and e >= mx:
                    it["full"] += 1
                frac = float(e / mx) if mx else 0
                if a.pk in upper:
                    frac_up.append(frac)
                if a.pk in lower:
                    frac_low.append(frac)
                if q.section == MCQ and ans is not None:
                    for i in (ans if isinstance(ans, list) else [ans]):
                        picks[i] += 1
                if q.section == FILL and ans is not None and e == 0:
                    wrongs[" ".join(ans.split()).casefold()] += 1
                if q.section == MATCH:
                    right = {right_id(q.id, i): i for i in range(len(q.payload.get("right", [])))}
                    for li, want in (key.get("pairs") or {}).items():
                        if right.get(((ans or {}).get(li))) == want:
                            pair_ok[li] += 1
            else:
                given = (a.open_marks or {}).get(str(q.id))
                if given is not None:
                    earned_all.append(Decimal(str(given)))
                    it["marked"] += 1
                elif ans is not None:
                    it["pending"] += 1
        if earned_all:
            it["avg"] = round(float(sum(earned_all) / len(earned_all)), 2)
        if q.section in OBJECTIVE_SECTIONS:
            it["pct_correct"] = _pct(it["full"], n)
            if frac_up and frac_low:
                it["disc"] = round(sum(frac_up) / len(frac_up) - sum(frac_low) / len(frac_low), 2)

        if q.section == MCQ:
            correct = correct_set(key)
            opts = q.payload.get("options", [])
            it["multi"] = is_multi(q)
            it["options"] = [{"text": o, "count": picks[i], "pct": _pct(picks[i], n), "correct": i in correct}
                             for i, o in enumerate(opts)]
            it["blank"] = n - it["answered"]
            wrong = [(picks[i], i) for i in range(len(opts)) if i not in correct]
            right_best = max([picks[i] for i in correct] or [0])
            if wrong:
                top = max(wrong)
                if top[0] > 0:
                    it["top_wrong"] = opts[top[1]]
                if n >= MIN_FOR_FLAGS and top[0] > right_best and not it["multi"]:
                    it["flags"].append(("danger", "More candidates chose a wrong option than the keyed answer. Check the key."))
        elif q.section == FILL:
            it["wrong_answers"] = wrongs.most_common(5)
            it["blank"] = n - it["answered"]
        elif q.section == MATCH:
            left = q.payload.get("left", [])
            it["pairs"] = [{"left": left[int(li)], "pct": _pct(pair_ok[li], n)} for li in sorted(key.get("pairs", {}), key=int)]

        if q.section in OBJECTIVE_SECTIONS and n >= MIN_FOR_FLAGS and it["pct_correct"] is not None:
            if it["pct_correct"] < 30:
                it["flags"].append(("warning", "Very hard: fewer than 30% got it fully right."))
            elif it["pct_correct"] > 90:
                it["flags"].append(("info", "Very easy: more than 90% got it right."))
            if it["disc"] is not None and it["disc"] < 0.1 and it["pct_correct"] <= 90:
                it["flags"].append(("warning", "Poor discrimination: strong and weak candidates did about equally well."))
        items.append(it)

    finals = [float(a.final_score) for a in attempts if a.final_score is not None]
    total = float(exam.total_marks)
    bins = []
    if finals and total:
        edges = [total * i / 10 for i in range(11)]
        for i in range(10):
            lo, hi = edges[i], edges[i + 1]
            c = sum(1 for f in finals if (lo <= f < hi) or (i == 9 and f == hi))
            bins.append({"label": f"{round(lo)}–{round(hi)}", "count": c})
        top = max(b["count"] for b in bins) or 1
        for b in bins:
            b["height"] = round(100 * b["count"] / top)
    summary = {"n": n, "in_progress": exam.attempts.filter(status=Attempt.IN_PROGRESS).count(),
               "mean": round(statistics.mean(finals), 2) if finals else None,
               "median": round(statistics.median(finals), 2) if finals else None,
               "high": round(max(finals), 2) if finals else None, "low": round(min(finals), 2) if finals else None,
               "pending_open": sum(1 for a in attempts if not a.marking_complete), "bins": bins, "total": total,
               "judged": n >= MIN_FOR_FLAGS}
    return summary, items
