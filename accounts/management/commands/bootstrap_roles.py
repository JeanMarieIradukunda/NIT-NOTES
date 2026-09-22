from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Creates the Trainer / Administrator groups if they don't exist."

    def handle(self, *args, **options):
        for name in (settings.ROLE_TRAINER, settings.ROLE_ADMIN):
            group, created = Group.objects.get_or_create(name=name)
            self.stdout.write(
                self.style.SUCCESS(f"  {'created' if created else 'exists '}  {name}"))
