"""
accounts.roles
===============

Two roles, implemented as Django Groups so they show up in /admin/ with no
extra machinery: Trainer and Administrator. Students have no account at
all — they browse, read and download the public library anonymously.

* Trainer       — adds module notes (PDF / HTML), and publishes, edits,
                   replaces, unpublishes and deletes the notes THEY added.
                   Works within the modules assigned to them by an
                   Administrator, plus any module they register themselves
                   while adding notes. Cannot see or touch another Trainer's
                   modules or notes.
* Administrator — full control, including creating and managing Trainer
                   accounts (Django superusers are always treated as
                   Administrators regardless of group membership).

Membership is checked with the helpers below rather than scattering
`request.user.groups.filter(...)` across every view.
"""

from django.conf import settings

TRAINER = settings.ROLE_TRAINER
ADMIN = settings.ROLE_ADMIN

ALL_ROLES = [TRAINER, ADMIN]


def user_roles(user):
    if not user.is_authenticated:
        return set()
    roles = set(user.groups.values_list("name", flat=True))
    if user.is_superuser:
        roles.add(ADMIN)
    return roles


def is_trainer(user):
    if not user.is_authenticated:
        return False
    return user.is_superuser or TRAINER in user_roles(user) or ADMIN in user_roles(user)


def is_admin(user):
    if not user.is_authenticated:
        return False
    return user.is_superuser or ADMIN in user_roles(user)


def can_upload(user):
    """Trainers and Administrators can upload lesson notes."""
    return is_trainer(user)


def can_manage(user, module=None):
    """
    Administrators manage everything. Trainers may only manage the modules
    they have been explicitly assigned to in their profile's
    `trainer_modules` — an unassigned Trainer manages nothing until an
    Administrator assigns them a module. This is deliberately strict: a
    Trainer must never be able to reach another Trainer's modules.

    Called with no `module` (e.g. "can this user upload at all") it answers
    whether the Trainer has *any* assigned modules to manage.
    """
    if is_admin(user):
        return True
    if not is_trainer(user):
        return False
    profile = getattr(user, "profile", None)
    if not profile:
        return False
    assigned = profile.trainer_modules.all()
    if module is None:
        return assigned.exists()
    return module in assigned


# --------------------------------------------------------------------------- #
# Module notes
# --------------------------------------------------------------------------- #

def can_add_notes(user):
    """Trainers and Administrators may add module notes."""
    return is_trainer(user)


def can_manage_note(user, note):
    """
    An Administrator manages every note. A Trainer manages only the notes they
    added themselves — never another Trainer's, even in a shared module.
    """
    if is_admin(user):
        return True
    return is_trainer(user) and note.uploaded_by_id is not None \
        and note.uploaded_by_id == user.id


def note_modules_for(user):
    """
    Modules the user may file notes under: every module for an Administrator,
    otherwise just the Trainer's own assigned modules.
    """
    from core.models import Module  # local import: core.middleware imports this module

    qs = Module.objects.select_related("trade").order_by("trade__order", "order", "code")
    if is_admin(user):
        return qs
    profile = getattr(user, "profile", None)
    if not is_trainer(user) or profile is None:
        return qs.none()
    return qs.filter(pk__in=profile.trainer_modules.values_list("pk", flat=True))
