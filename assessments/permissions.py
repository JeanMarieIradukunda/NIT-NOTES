from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render

from accounts.roles import is_admin, is_trainer


def can_manage_exam(user, exam):
    """Administrators manage every exam; a Trainer manages only the exams they created."""
    if is_admin(user):
        return True
    return is_trainer(user) and exam.created_by_id is not None and exam.created_by_id == user.id


def deny(request, title, detail):
    return render(request, "core/error_panel.html", {"title": title, "detail": detail}, status=403)


def trainer_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_trainer(request.user):
            return deny(request, "Trainers only",
                        "Creating and managing assessments is available to Trainers and Administrators.")
        return view(request, *args, **kwargs)
    return wrapper
