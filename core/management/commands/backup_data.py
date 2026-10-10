"""
python manage.py backup_data [--output-dir backups] [--keep 10]

Writes a JSON snapshot of the database (users, modules, notes' records, assessments, results ...)
to backups/nit-YYYYMMDD-HHMMSS.json and keeps the newest N files.

What it does NOT contain: the uploaded files themselves (PDFs and HTML notes live in file storage /
Vercel Blob, not the database). Restore the database with `python manage.py loaddata <file>` on a
migrated, empty database. Treat the file as confidential: it holds password hashes and candidate results.
"""

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Write a JSON backup of the database and prune old backups."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="backups", help="Folder for backups (default: backups/).")
        parser.add_argument("--keep", type=int, default=10, help="How many recent backups to keep (default 10).")

    def handle(self, *args, output_dir, keep, **opts):
        folder = Path(output_dir)
        if not folder.is_absolute():
            folder = Path(settings.BASE_DIR) / folder
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"nit-{datetime.now():%Y%m%d-%H%M%S}.json"
        with open(target, "w", encoding="utf-8") as fh:
            call_command("dumpdata", natural_foreign=True, natural_primary=True, indent=1,
                         exclude=["contenttypes", "auth.permission", "sessions", "admin.logentry",
                                  "accounts.loginfailure"], stdout=fh)
        for old in sorted(folder.glob("nit-*.json"))[:-max(keep, 1)]:
            old.unlink()
        self.stdout.write(self.style.SUCCESS(f"Backup written: {target} ({target.stat().st_size // 1024} KB)"))
