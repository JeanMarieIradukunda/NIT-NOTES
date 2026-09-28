"""
python manage.py check_blob [--diagnose]

Verifies that file storage works end to end against your real Vercel Blob
store: uploads a tiny probe file, reads it back, then deletes it. Reports which
access mode (public / private) your store uses. On failure it prints a
request-by-request report (never the token) showing exactly where it broke.
"""

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from core import storage as blob


class Command(BaseCommand):
    help = "Upload, read back and delete a probe file to verify Vercel Blob storage."

    def add_arguments(self, parser):
        parser.add_argument("--diagnose", action="store_true",
                            help="Always print the request-level report, even on success.")

    def _report(self, store, name):
        self.stdout.write("Diagnostics:")
        for line in store.diagnose(name):
            self.stdout.write(f"  {line}")

    def handle(self, *args, **options):
        if not getattr(settings, "BLOB_READ_WRITE_TOKEN", ""):
            raise CommandError("BLOB_READ_WRITE_TOKEN is not set, so local disk storage is in use. "
                               "Nothing to check.")
        store = blob.VercelBlobStorage(prefix="private_media")
        payload = b"%PDF-1.4\nnit-resources blob probe\n"
        self.stdout.write(f"Store id: {store.store_id or '(unknown)'}  "
                          f"BLOB_ACCESS: {blob.configured_access()}")
        name = None
        try:
            name = store.save("healthcheck/probe.pdf", ContentFile(payload))
            self.stdout.write(self.style.SUCCESS(f"Uploaded  : {name}"))
            put = getattr(store, "last_put", None)
            if put:
                self.stdout.write(f"Upload response: access={put['access']} "
                                  f"pathname={put['pathname']} url={put['url']}")
            if options["diagnose"]:
                self._report(store, name)
            data = store.open(name).read()
            if data != payload:
                raise CommandError("Read back different bytes than were uploaded.")
            self.stdout.write(self.style.SUCCESS("Read back : OK (bytes match)"))
            learned = blob._learned_access.get(store.store_id) or blob.configured_access()
            self.stdout.write(f"Access mode in use: {learned}")
        except OSError as exc:
            if name:
                self._report(store, name)
            raise CommandError(f"Vercel Blob check failed ({type(exc).__name__}): {exc}")
        finally:
            if name:
                store.delete(name)
                self.stdout.write("Probe file deleted.")
        self.stdout.write(self.style.SUCCESS("Vercel Blob storage is working."))
