"""
manage.py purge_module_data

Removes every module-related record and file so the platform starts from a
clean, empty module database — while leaving the platform itself intact:

  removed   Modules, Units, Lessons, Resources, Activities, Trainer module
            notes, upload records, student reading activity (bookmarks /
            progress), the admin-log entries about those objects, and the
            files behind them (media/legacy, media/resources,
            media/lesson_uploads, media/activities and the private
            module-notes folder). Trainers' module assignments go with the
            modules they pointed at.

  kept      Levels (Trade rows — needed to file a new module under), user
            accounts, groups/roles, and all application code and structure.
            Pass --include-levels to remove the levels as well.

Destructive and irreversible, so it lists exactly what it will delete and asks
you to confirm (or pass --yes for scripted use). Safe to re-run.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction

from core.models import (Activity, Lesson, LessonUpload, Module, ModuleNote,
                         Resource, StudentActivity, Trade, Unit)

# Deletion order: children first, so the report reads naturally.
MODEL_ORDER = [ModuleNote, StudentActivity, LessonUpload, Activity, Lesson,
              Resource, Unit, Module]


def _file_dirs():
    media, private = Path(settings.MEDIA_ROOT), Path(settings.PRIVATE_MEDIA_ROOT)
    return [(media, media / "legacy"), (media, media / "resources"),
            (media, media / "lesson_uploads"), (media, media / "activities"),
            (private, private / "module_notes")]


class Command(BaseCommand):
    help = "Deletes all module data (modules, units, lessons, resources, notes) and their files."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would be deleted, change nothing.")
        parser.add_argument("--include-levels", action="store_true",
                            help="Also delete the levels (Trade rows).")

    def handle(self, *args, yes, dry_run, include_levels, **options):
        models = list(MODEL_ORDER) + ([Trade] if include_levels else [])

        self.stdout.write(self.style.MIGRATE_HEADING("Module data currently in the database:"))
        for model in models:
            self.stdout.write(f"  {model._meta.verbose_name_plural:<24} {model.objects.count():>6}")
        dirs = [d for _, d in _file_dirs() if d.exists()]
        for d in dirs:
            n = sum(1 for f in d.rglob("*") if f.is_file())
            self.stdout.write(f"  files in {str(d):<40} {n:>6}")
        if not include_levels:
            self.stdout.write(f"  (levels kept: {Trade.objects.count()} — use --include-levels to remove)")

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run — nothing was changed."))
            return

        if not yes:
            self.stdout.write(self.style.WARNING(
                "\nThis permanently deletes the data above. User accounts are not touched."))
            if input("Type 'delete' to continue: ").strip().lower() != "delete":
                raise CommandError("Aborted — nothing was changed.")

        with transaction.atomic():
            for model in models:
                model.objects.all().delete()   # per-instance, so file-cleanup signals fire

            cts = ContentType.objects.get_for_models(*models).values()
            LogEntry.objects.filter(content_type__in=cts).delete()
            self._reset_sequences(models)

        removed_files = 0
        for root, d in _file_dirs():
            if not d.exists():
                continue
            # Never delete anything outside the configured roots.
            if root.resolve() not in d.resolve().parents:
                raise CommandError(f"Refusing to delete {d}: not inside {root}")
            removed_files += sum(1 for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)

        # Give the freed space back to the SQLite file. VACUUM can't run inside a
        # transaction, so skip it quietly if we were called from within one.
        if connection.vendor == "sqlite" and not connection.in_atomic_block:
            with connection.cursor() as cursor:
                cursor.execute("VACUUM")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Module data removed ({removed_files} file(s) deleted). "
            "The module database is now empty."))

    @staticmethod
    def _reset_sequences(models):
        """Restart ID numbering at 1 so the empty database really looks new."""
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                tables = [m._meta.db_table for m in models]
                marks = ", ".join(["%s"] * len(tables))
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({marks})", tables)
            else:
                for sql in connection.ops.sequence_reset_sql(no_style(), models):
                    cursor.execute(sql)
