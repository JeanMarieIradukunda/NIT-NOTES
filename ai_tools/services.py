"""
ai_tools.services
===================

A tiny, deliberately boring abstraction over "call an LLM with a prompt and
get text back." Two features are built on it:

  * Summarise this lesson — a short study summary generated on demand.
  * Ask about this lesson — a question answered using only the lesson's own
    text as context, so answers stay grounded in the curriculum rather than
    the model's general knowledge.

Groq is the platform's only AI provider — every AI-powered feature in the
system goes through `complete()` below. Nothing here requires a key to be
set. Without one, `is_configured()` is False and the views show a clear
explanation instead of failing.
"""

from __future__ import annotations

from django.conf import settings


class AIError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.GROQ_API_KEY)


def _complete_groq(system: str, user: str) -> str:
    try:
        from groq import Groq
    except ImportError as exc:
        raise AIError(
            "The 'groq' package is not installed. Run "
            "`pip install groq` to enable AI features.") from exc

    client = Groq(api_key=settings.GROQ_API_KEY)
    try:
        response = client.chat.completions.create(
            model=settings.GROQ_MODEL,
            max_tokens=700,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:  # network issues, bad key, rate limits…
        raise AIError(f"The AI service could not be reached ({exc}).") from exc

    choice = response.choices[0].message.content if response.choices else ""
    return (choice or "").strip()


def complete(system: str, user: str) -> str:
    if not is_configured():
        raise AIError(
            "AI features are turned off because no API key is configured. "
            "Set GROQ_API_KEY in the server's environment to enable them — "
            "see the README.")
    return _complete_groq(system, user)


# --------------------------------------------------------------------------- #
# Feature-level helpers — what the views actually call.
# --------------------------------------------------------------------------- #

SUMMARY_SYSTEM = (
    "You are a study assistant for vocational IT students. Summarise the "
    "lesson notes you are given into a short, exam-focused revision summary: "
    "a 2-3 sentence overview, then 4-8 bullet points of the key facts, "
    "terms and steps a student must remember. Use plain language. Do not "
    "invent facts that aren't in the notes. Use Markdown."
)

TUTOR_SYSTEM = (
    "You are a patient tutor helping a vocational IT student understand one "
    "specific lesson. You are given the lesson's notes as context and a "
    "student's question. Answer using only what is in the notes plus "
    "well-established, uncontroversial technical facts needed to explain "
    "them — do not introduce a different curriculum's approach. If the "
    "notes don't cover something the student asks, say so plainly rather "
    "than guessing. Keep answers concise and use Markdown."
)


def summarise_lesson(title: str, plain_text: str) -> str:
    prompt = f"Lesson: {title}\n\nNotes:\n{plain_text[:6000]}"
    return complete(SUMMARY_SYSTEM, prompt)


def answer_question(title: str, plain_text: str, question: str) -> str:
    prompt = (f"Lesson: {title}\n\nNotes:\n{plain_text[:6000]}\n\n"
             f"Student's question: {question}")
    return complete(TUTOR_SYSTEM, prompt)
