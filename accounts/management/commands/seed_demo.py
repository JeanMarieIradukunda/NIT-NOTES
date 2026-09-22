"""
manage.py seed_demo

Creates a superuser and one demo Trainer account so a fresh clone can be
explored immediately. Students don't get accounts, so there's no demo
student. Safe to re-run — existing users are left alone.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

User = get_user_model()

ACCOUNTS = [
    ("admin", "admin@example.com", "Administrator", None, True),
    ("trainer_demo", "trainer@example.com", "Demo Trainer", "Trainer", False),
]
DEMO_PASSWORD = "ChangeMe!2026"


class Command(BaseCommand):
    help = "Creates demo admin / trainer accounts for evaluation."

    def handle(self, *args, **options):
        for username, email, name, role, is_super in ACCOUNTS:
            if User.objects.filter(username=username).exists():
                self.stdout.write(f"  exists   {username}")
                continue
            if is_super:
                user = User.objects.create_superuser(username, email, DEMO_PASSWORD)
            else:
                user = User.objects.create_user(username, email, DEMO_PASSWORD,
                                                 is_active=True)
            user.first_name = name
            user.save()
            if role:
                group, _ = Group.objects.get_or_create(name=role)
                user.groups.set([group])
            self.stdout.write(self.style.SUCCESS(f"  created  {username}  ({role or 'Administrator'})"))

        self.stdout.write(self.style.WARNING(
            f"\nDemo password for every account: {DEMO_PASSWORD}"
            "\nChange these before deploying anywhere reachable by the public."))
