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

from .models import FILL, MATCH, MCQ, OBJECTIVE_SECTIONS, OPEN, SECTION_ORDER

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
# Candidate-facing questions  (NO answer key is read here)
# --------------------------------------------------------------------------- #

def public_questions(exam, attempt):
    out = []
    qs = list(exam.questions.all())
    qs.sort(key=lambda q: (SECTION_ORDER.index(q.section), q.order, q.id))
    for q in qs:
        item = {"id": q.id, "section": q.section, "text": q.text}
        if q.section == MCQ:
            item["options"] = list(q.payload.get("options", []))
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
        try:
            i = int(value)
        except (TypeError, ValueError):
            return None
        return i if 0 <= i < len(question.payload.get("options", [])) else None
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


def mark_question(question, key_data, answer, max_marks):
    """Marks earned for one objective question (fraction of max_marks)."""
    answer = clean_answer(question, answer)
    if answer is None:
        return Decimal("0")
    if question.section == MCQ:
        return max_marks if answer == key_data.get("correct") else Decimal("0")
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
        earned = mark_question(q, key.data if key else {}, (answers or {}).get(str(q.id)), maxima[q.id])
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
