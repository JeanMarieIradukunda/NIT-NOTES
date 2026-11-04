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

from . import charts
from .marking import (OVERRIDABLE_SECTIONS, _canonical, clean_answer, correct_set, is_multi, mark_question, ordered_questions,
                      question_max_marks, right_id)
from .models import FILL, MATCH, MCQ, OBJECTIVE_SECTIONS, OPEN, SECTION_LABELS, Attempt

MIN_FOR_FLAGS = 5            # fewer submitted attempts than this: figures are shown but not judged
MIN_FOR_DISCRIMINATION = 10


def _pct(n, d):
    return round(100 * n / d) if d else None


BAND_LABELS = {"vhard": "Very hard", "hard": "Hard", "ok": "Balanced", "easy": "Easy"}


def band_for(pct):
    """How demanding a question was, from the share of its marks candidates earned."""
    if pct is None:
        return ""
    return "vhard" if pct < 30 else "hard" if pct < 50 else "ok" if pct < 85 else "easy"


def disc_label(d):
    if d is None:
        return ""
    return "Good" if d >= 0.3 else "Fair" if d >= 0.1 else "Weak" if d >= 0 else "Reversed"


def _enrich_item(it):
    """Plain-language fields for the page and the charts. Adds to the item, never changes existing keys."""
    mx = float(it["max"]) if it["max"] else 0
    it["score_pct"] = round(100 * it["avg"] / mx) if (it["avg"] is not None and mx) else None
    it["band"] = band_for(it["score_pct"])
    it["band_label"] = BAND_LABELS.get(it["band"], "Not marked yet" if it["section"] == OPEN else "No data")
    it["disc_label"] = disc_label(it["disc"])
    it["needs_review"] = any(kind in ("danger", "warning") for kind, _msg in it["flags"])
    it["short"] = " ".join(it["text"].split())[:90]


def _questions(nums):
    shown = ", ".join(f"Q{n}" for n in nums[:8])
    return shown + (f" and {len(nums) - 8} more" if len(nums) > 8 else "")


def _insights(summary, items):
    """Findings in plain English, most important first. Every line is a fact the numbers support."""
    out = []
    n = summary["n"]
    if summary["mean_pct"] is not None:
        out.append(("info", f"Candidates averaged {summary['mean_pct']}% (median {summary['median_pct']}%), with scores "
                            f"from {summary['low_pct']}% to {summary['high_pct']}%."))
    if not summary["judged"]:
        out.append(("warning", f"Only {n} submitted so far. Questions are not judged until {MIN_FOR_FLAGS} candidates have submitted."))
        return out
    if summary["std_pct"] is not None:
        if summary["std_pct"] < 10:
            out.append(("warning", f"Scores are tightly bunched (standard deviation {summary['std_pct']} points), "
                                   "so the paper separates candidates only weakly."))
        elif summary["std_pct"] > 25:
            out.append(("info", f"Scores are widely spread (standard deviation {summary['std_pct']} points): "
                                "candidates differ a lot in what they know."))
    danger = [it for it in items if any(k == "danger" for k, _ in it["flags"])]
    for it in danger[:3]:
        out.append(("danger", f"Q{it['n']}: more candidates chose a wrong option than the keyed answer. Check the answer key."))
    vhard = [it["n"] for it in items if it["band"] == "vhard" and it["n"] not in {d["n"] for d in danger}]
    if vhard:
        out.append(("warning", f"{len(vhard)} question{'s were' if len(vhard) != 1 else ' was'} very hard "
                               f"(under 30% of the marks earned): {_questions(vhard)}. Check the topic was taught and the wording is clear."))
    weak = [it["n"] for it in items if it["disc"] is not None and it["disc"] < 0.1 and it["band"] != "easy"]
    if weak:
        out.append(("warning", f"{_questions(weak)} did not separate strong from weak candidates (discrimination under 0.1)."))
    easy = [it["n"] for it in items if it["band"] == "easy"]
    if easy:
        out.append(("info", f"{len(easy)} question{'s were' if len(easy) != 1 else ' was'} easy "
                            f"(85% or more of the marks earned): {_questions(easy)}."))
    if summary["pending_open"]:
        out.append(("warning", f"{summary['pending_open']} attempt{'s' if summary['pending_open'] != 1 else ''} still have open "
                               "questions to mark, so scores and open-question averages are provisional."))
    if len(out) == 1 or all(k == "info" for k, _ in out[1:]):
        out.append(("good", "No questions need review: none was flagged as mis-keyed, very hard or poorly discriminating."))
    return out


def _overall(mean_pct):
    if mean_pct is None:
        return None
    if mean_pct < 40:
        return {"label": "Hard paper", "kind": "warning", "text": "Candidates earned under 40% of the marks on average."}
    if mean_pct < 70:
        return {"label": "Well balanced", "kind": "good", "text": "Average between 40% and 70%: room for both weaker and stronger candidates."}
    return {"label": "Easy paper", "kind": "info", "text": "Candidates earned 70% or more of the marks on average."}


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
        _enrich_item(it)
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
    pct = (lambda v: round(100 * v / total, 1)) if total else (lambda v: None)
    summary.update({
        "mean_pct": pct(statistics.mean(finals)) if finals else None,
        "median_pct": pct(statistics.median(finals)) if finals else None,
        "high_pct": pct(max(finals)) if finals else None, "low_pct": pct(min(finals)) if finals else None,
        "std": round(statistics.pstdev(finals), 2) if len(finals) > 1 else None,
        "std_pct": pct(statistics.pstdev(finals)) if len(finals) > 1 else None,
    })
    summary["overall"] = _overall(summary["mean_pct"])

    # performance by question type: marks earned as a share of marks available
    sections = []
    for key in ("mcq", "fill", "match", "open"):
        group = [it for it in items if it["section"] == key and it["avg"] is not None and it["max"]]
        if group:
            earned, avail = sum(it["avg"] for it in group), sum(float(it["max"]) for it in group)
            p = round(100 * earned / avail)
            sections.append({"key": key, "label": SECTION_LABELS[key], "questions": len(group), "pct": p, "band": band_for(p)})
    summary["sections"] = sections
    summary["mix"] = [{"band": b, "label": BAND_LABELS[b], "count": sum(1 for it in items if it["band"] == b)}
                      for b in ("vhard", "hard", "ok", "easy")]
    summary["mix_total"] = sum(m["count"] for m in summary["mix"])
    for m in summary["mix"]:
        m["pct"] = round(100 * m["count"] / summary["mix_total"]) if summary["mix_total"] else 0
    summary["attention"] = [{"n": it["n"], "short": it["short"], "band": it["band"], "reasons": [m for _k, m in it["flags"]]}
                            for it in items if it["needs_review"]]
    summary["insights"] = _insights(summary, items) if n else []
    summary["hist"] = charts.histogram(bins, total, mean=summary["mean"], median=summary["median"]) if bins else None
    summary["scatter"] = charts.scatter(items)
    return summary, items
