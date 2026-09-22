from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import Group
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ProfileForm, TrainerCreateForm, TrainerModulesForm
from .roles import is_admin, user_roles

User = get_user_model()


# --------------------------------------------------------------------------- #
# Trainer management — Administrator only. There is no public registration:
# Students never get accounts, and only an Administrator can create or
# change a Trainer account.
# --------------------------------------------------------------------------- #

@login_required
@user_passes_test(is_admin, login_url="accounts:login")
def manage_trainers(request):
    trainers = (User.objects.filter(groups__name="Trainer")
                .select_related("profile")
                .prefetch_related("profile__trainer_modules")
                .order_by("first_name", "username"))
    return render(request, "accounts/manage_trainers.html", {"trainers": trainers})


@login_required
@user_passes_test(is_admin, login_url="accounts:login")
def create_trainer(request):
    if request.method == "POST":
        form = TrainerCreateForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.email = form.cleaned_data["email"]
            user.first_name = form.cleaned_data["first_name"]
            user.is_active = True
            user.save()

            trainer_group, _ = Group.objects.get_or_create(name="Trainer")
            user.groups.set([trainer_group])

            profile = user.profile
            profile.trainer_modules.set(form.cleaned_data["modules"])

            messages.success(
                request,
                f"Trainer account created for {user.first_name or user.username}. "
                f"They can now sign in and manage {profile.trainer_modules.count()} "
                "assigned module(s).")
            return redirect("accounts:manage_trainers")
    else:
        form = TrainerCreateForm()

    return render(request, "accounts/trainer_form.html", {"form": form, "trainer": None})


@login_required
@user_passes_test(is_admin, login_url="accounts:login")
def edit_trainer_modules(request, user_id):
    trainer = get_object_or_404(User.objects.select_related("profile"),
                                 pk=user_id, groups__name="Trainer")
    if request.method == "POST":
        form = TrainerModulesForm(request.POST)
        if form.is_valid():
            trainer.profile.trainer_modules.set(form.cleaned_data["modules"])
            messages.success(request, f"Updated modules for {trainer.first_name or trainer.username}.")
            return redirect("accounts:manage_trainers")
    else:
        form = TrainerModulesForm(initial={
            "modules": trainer.profile.trainer_modules.values_list("pk", flat=True),
        })

    return render(request, "accounts/trainer_modules_form.html", {
        "form": form, "trainer": trainer,
    })


@login_required
@user_passes_test(is_admin, login_url="accounts:login")
def toggle_trainer_active(request, user_id):
    trainer = get_object_or_404(User, pk=user_id, groups__name="Trainer")
    if request.method == "POST":
        trainer.is_active = not trainer.is_active
        trainer.save(update_fields=["is_active"])
        messages.success(
            request,
            f"{trainer.first_name or trainer.username} is now "
            f"{'active' if trainer.is_active else 'suspended'}.")
    return redirect("accounts:manage_trainers")


# --------------------------------------------------------------------------- #
# Every signed-in user's own profile (Trainers and Administrators only).
# --------------------------------------------------------------------------- #

@login_required
def profile(request):
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user.profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("accounts:profile")
    else:
        form = ProfileForm(instance=request.user.profile)

    return render(request, "accounts/profile.html", {
        "form": form,
        "roles": sorted(user_roles(request.user)),
        "managed_modules": (request.user.profile.trainer_modules.select_related("trade")
                             if hasattr(request.user, "profile") else []),
    })
