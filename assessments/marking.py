"""
assessments.marking
===================

Question serialisation for candidates, answer validation, and marking.

The only module that reads AnswerKey. `public_questions()` — the function that
builds the candidate page — never touches it.
"""

import random
import re
from decimal import ROUND_HALF_UP, Decimal

from django.utils.crypto import salted_hmac

from .models import FILL, MATCH, MCQ, OBJECTIVE_SECTIONS, OPEN, SECTION_LABELS, SECTION_ORDER

TWO = Decimal("0.01")
MAX_OPEN_CHARS = 20000
MAX_FILL_CHARS = 500


def q2(value):
    return Decimal(str(value)).quantize(TWO, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Marks available per question
# --------------------------------------------------------------------------- #

def question_max_marks(exam, questions=None):
    """{question id: Decimal}. A section's marks are shared between its questions by weight."""
    questions = list(questions if questions is not None else exam.questions.all())
    weight_sum = {}
    for q in questions:
        weight_sum[q.section] = weight_sum.get(q.section, 0) + max(q.weight, 1)
    out = {}
    for q in questions:
        total = Decimal(weight_sum[q.section])
        out[q.id] = q2(exam.section_marks(q.section) * Decimal(max(q.weight, 1)) / total)
    return out


# --------------------------------------------------------------------------- #
# Opaque ids for matching choices (so the page never hints at the pairing)
# --------------------------------------------------------------------------- #

def right_id(question_id, index):
    return salted_hmac("assessments.match", f"{question_id}:{index}").hexdigest()[:8]


def _right_index_for(question, rid):
    for i in range(len(question.payload.get("right", []))):
        if right_id(question.id, i) == rid:
            return i
    return None


# --------------------------------------------------------------------------- #
# Multiple choice: one answer, several accepted answers, or "select all"
# --------------------------------------------------------------------------- #

def correct_set(key):
    """The correct option indexes. Reads the old single-int key and the new list."""
    c = (key or {}).get("correct")
    if c is None or isinstance(c, bool):
        return set()
    if isinstance(c, int):
        return {c}
    return {i for i in c if isinstance(i, int) and not isinstance(i, bool)}


def is_multi(question):
    """True for "select all that apply": candidates tick several boxes."""
    return bool(question.payload.get("multi"))


# --------------------------------------------------------------------------- #
# Per-candidate order (question and option shuffling)
# --------------------------------------------------------------------------- #

KEEP_IN_ORDER_RE = re.compile(
    r"\b(?:all|none|both|neither|any)\s+of\s+(?:the\s+)?(?:above|these|them|the\s+following)\b"
    r"|\ball\s+the\s+above\b|\bboth\s+[a-h]\s+and\s+[a-h]\b", re.I)


def _canonical(questions):
    return sorted(questions, key=lambda q: (SECTION_ORDER.index(q.section), q.order, q.id))


def _option_idx(exam, attempt, q):
    n = len(q.payload.get("options", []))
    idx = list(range(n))
    if exam.shuffle_options and not any(KEEP_IN_ORDER_RE.search(o) for o in q.payload.get("options", [])):
        random.Random(f"o:{attempt.pk}:{q.pk}").shuffle(idx)
    return idx


def build_layout(exam, attempt, questions):
    order = []
    for s in SECTION_ORDER:
        ids = [q.id for q in _canonical(questions) if q.section == s]
        if exam.shuffle_questions:
            random.Random(f"q:{attempt.pk}:{s}").shuffle(ids)
        order += ids
    opts = {str(q.id): _option_idx(exam, attempt, q) for q in questions if q.section == MCQ} if exam.shuffle_options else {}
    return {"q": exam.shuffle_questions, "o": exam.shuffle_options, "order": order, "opts": opts}


def persist_layout(exam, attempt):
    """Fix this candidate's paper at first open so later edits cannot reorder it."""
    if not (exam.shuffle_questions or exam.shuffle_options):
        return
    lay = attempt.layout or {}
    if lay.get("q") == exam.shuffle_questions and lay.get("o") == exam.shuffle_options and lay.get("order"):
        return
    attempt.layout = build_layout(exam, attempt, list(exam.questions.all()))
    attempt.save(update_fields=["layout", "updated_at"])


def ordered_questions(exam, attempt, questions=None):
    """The questions in the order THIS candidate saw them. Used by every screen so numbering always agrees."""
    qs = list(questions) if questions is not None else list(exam.questions.select_related("key"))
    canon = _canonical(qs)
    if not exam.shuffle_questions:
        return canon
    lay = attempt.layout or {}
    order = lay.get("order") if lay.get("q") else build_layout(exam, attempt, qs)["order"]
    pos = {qid: i for i, qid in enumerate(order or [])}
    canon_pos = {q.id: i for i, q in enumerate(canon)}
    return sorted(canon, key=lambda q: (SECTION_ORDER.index(q.section), pos.get(q.id, 10**6 + canon_pos[q.id])))


def option_order(exam, attempt, q):
    """Original option indexes in the order this candidate saw them."""
    n = len(q.payload.get("options", []))
    lay = attempt.layout or {}
    idx = (lay.get("opts") or {}).get(str(q.id)) if (lay.get("o") and exam.shuffle_options) else None
    if idx and sorted(idx) == list(range(n)):
        return idx
    return _option_idx(exam, attempt, q)


# --------------------------------------------------------------------------- #
# Candidate-facing questions  (NO answer key is read here)
# --------------------------------------------------------------------------- #

def public_questions(exam, attempt):
    out = []
    for q in ordered_questions(exam, attempt):
        item = {"id": q.id, "section": q.section, "text": q.text}
        if q.section == MCQ:
            opts = list(q.payload.get("options", []))
            idx = option_order(exam, attempt, q)
            item["options"] = [opts[i] for i in idx]
            item["optionIdx"] = idx                     # value saved for each displayed option
            item["multi"] = is_multi(q)
        elif q.section == MATCH:
            left = list(q.payload.get("left", []))
            right = list(q.payload.get("right", []))
            idx = list(range(len(right)))
            random.Random(f"{attempt.pk}:{q.pk}").shuffle(idx)   # stable between reopens
            item["left"] = left
            item["right"] = [{"id": right_id(q.id, i), "text": right[i]} for i in idx]
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Answer validation
# --------------------------------------------------------------------------- #

def clean_answer(question, value):
    """Normalise one answer from the browser. Returns None for 'no answer'."""
    if value is None:
        return None
    if question.section == MCQ:
        n = len(question.payload.get("options", []))
        if is_multi(question):
            if isinstance(value, int) and not isinstance(value, bool):
                value = [value]
            if not isinstance(value, (list, tuple)):
                return None
            picked = sorted({i for i in value if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n})
            return picked or None
        if isinstance(value, (list, tuple, dict, bool)):
            return None
        try:
            i = int(value)
        except (TypeError, ValueError):
            return None
        return i if 0 <= i < n else None
    if question.section in (FILL, OPEN):
        if not isinstance(value, str):
            return None
        limit = MAX_FILL_CHARS if question.section == FILL else MAX_OPEN_CHARS
        value = value[:limit]
        return value if value.strip() else None
    if question.section == MATCH:
        if not isinstance(value, dict):
            return None
        left_n = len(question.payload.get("left", []))
        valid_rights = {right_id(question.id, i) for i in range(len(question.payload.get("right", [])))}
        cleaned = {}
        for k, v in value.items():
            if str(k).isdigit() and 0 <= int(k) < left_n and v in valid_rights:
                cleaned[str(int(k))] = v
        return cleaned or None
    return None


def is_answered(question, value):
    return clean_answer(question, value) is not None


# --------------------------------------------------------------------------- #
# Marking
# --------------------------------------------------------------------------- #

def _norm(text, case_sensitive):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if case_sensitive else text.casefold()


def mark_question(question, key_data, answer, max_marks, scoring="partial"):
    """Marks earned for one objective question (fraction of max_marks)."""
    answer = clean_answer(question, answer)
    if answer is None:
        return Decimal("0")
    if question.section == MCQ:
        correct = correct_set(key_data)
        if not correct:
            return Decimal("0")
        if not is_multi(question):
            return max_marks if answer in correct else Decimal("0")     # any listed answer is accepted
        chosen = set(answer)
        if scoring == "all":
            return max_marks if chosen == correct else Decimal("0")
        net = len(chosen & correct) - len(chosen - correct)
        return q2(max_marks * Decimal(max(net, 0)) / Decimal(len(correct)))
    if question.section == FILL:
        cs = bool(key_data.get("case_sensitive"))
        given = _norm(answer, cs)
        accepted = {_norm(a, cs) for a in key_data.get("accepted", [])}
        return max_marks if given in accepted else Decimal("0")
    if question.section == MATCH:
        pairs = key_data.get("pairs", {})
        if not pairs:
            return Decimal("0")
        right = 0
        for left, rid in answer.items():
            if _right_index_for(question, rid) == pairs.get(left):
                right += 1
        return q2(max_marks * Decimal(right) / Decimal(len(pairs)))
    return Decimal("0")


def mark_objective(exam, answers):
    """
    Marks MCQ, fill and matching against the server-side key.
    Returns (total, {question id: marks}, {section: marks}).
    """
    questions = list(exam.questions.select_related("key"))
    maxima = question_max_marks(exam, questions)
    per_q, per_section = {}, {s: Decimal("0") for s in OBJECTIVE_SECTIONS}
    for q in questions:
        if q.section not in OBJECTIVE_SECTIONS:
            continue
        key = getattr(q, "key", None)
        earned = mark_question(q, key.data if key else {}, (answers or {}).get(str(q.id)), maxima[q.id],
                               exam.multi_scoring)
        per_q[q.id] = earned
        per_section[q.section] += earned
    total = sum(per_section.values(), Decimal("0"))
    return q2(total), per_q, {s: q2(v) for s, v in per_section.items()}


def recompute_final(attempt):
    """final = max(0, objective + marked open questions - penalty). Never below zero."""
    gross = Decimal(attempt.objective_score) + attempt.open_total
    attempt.final_score = q2(max(Decimal("0"), gross - Decimal(attempt.penalty_total)))
    return attempt.final_score


def has_open_questions(exam):
    return exam.questions.filter(section=OPEN).exists()


def review_questions(exam, attempt):
    """
    Per-question review for a SUBMITTED attempt: the candidate's answer, the
    correct answer and the marks, in the order the candidate saw. Only called
    when the trainer has enabled show_answers; the answer key is read here and
    nowhere on the candidate's exam page.
    """
    qs = ordered_questions(exam, attempt)
    maxima = question_max_marks(exam, qs)
    rows = []
    for n, q in enumerate(qs, start=1):
        key = q.key.data if hasattr(q, "key") else {}
        ans = clean_answer(q, (attempt.answers or {}).get(str(q.id)))
        row = {"n": n, "section": q.section, "label": SECTION_LABELS[q.section], "text": q.text,
               "max": maxima[q.id], "answered": ans is not None, "earned": None, "status": "blank"}
        if q.section == MCQ:
            chosen = set(ans) if isinstance(ans, list) else ({ans} if ans is not None else set())
            correct = correct_set(key)
            opts = q.payload.get("options", [])
            row["multi"] = is_multi(q)
            row["accept_any"] = (not is_multi(q)) and len(correct) > 1
            row["options"] = [{"text": opts[i], "chosen": i in chosen, "correct": i in correct}
                              for i in option_order(exam, attempt, q)]
        elif q.section == FILL:
            row["given"] = ans or ""
            row["accepted"] = key.get("accepted", [])
        elif q.section == MATCH:
            right = {right_id(q.id, i): t for i, t in enumerate(q.payload.get("right", []))}
            pairs = key.get("pairs", {})
            row["pairs"] = []
            for i, left in enumerate(q.payload.get("left", [])):
                given = right.get((ans or {}).get(str(i)), "")
                correct = q.payload["right"][pairs[str(i)]] if str(i) in pairs else ""
                row["pairs"].append({"left": left, "given": given, "correct": correct, "ok": bool(given) and given == correct})
        elif q.section == OPEN:
            row["given"] = ans or ""
            row["guide"] = key.get("guide", "")
        if q.section in OBJECTIVE_SECTIONS:
            earned = mark_question(q, key, (attempt.answers or {}).get(str(q.id)), maxima[q.id], exam.multi_scoring)
            row["earned"] = earned
            row["status"] = ("correct" if earned >= maxima[q.id] and earned > 0 else
                             "partial" if earned > 0 else "wrong" if ans is not None else "blank")
        else:
            given = (attempt.open_marks or {}).get(str(q.id))
            if given is not None:
                row["earned"] = Decimal(str(given))
                row["status"] = "marked"
            else:
                row["status"] = "pending" if ans is not None else "blank"
        rows.append(row)
    return rows
