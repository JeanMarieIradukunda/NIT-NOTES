import json

import markdown as md_lib
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from core.models import Lesson
from . import services


def _markdown(text: str) -> str:
    return md_lib.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])


@login_required
@require_POST
def summarise(request, slug):
    lesson = get_object_or_404(Lesson, slug=slug, is_published=True)
    try:
        summary = services.summarise_lesson(lesson.title, lesson.search_text)
    except services.AIError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    return JsonResponse({"ok": True, "html": _markdown(summary)})


@login_required
@require_POST
def ask(request, slug):
    lesson = get_object_or_404(Lesson, slug=slug, is_published=True)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}
    question = (payload.get("question") or request.POST.get("question", "")).strip()

    if not question:
        return JsonResponse({"ok": False, "error": "Type a question first."}, status=400)
    if len(question) > 600:
        return JsonResponse({"ok": False, "error": "That question is too long."}, status=400)

    try:
        answer = services.answer_question(lesson.title, lesson.search_text, question)
    except services.AIError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    return JsonResponse({"ok": True, "html": _markdown(answer)})


def status(request):
    """Lets the front end know, once, whether to show the AI panel at all."""
    return JsonResponse({"enabled": services.is_configured(), "provider": "groq"})
